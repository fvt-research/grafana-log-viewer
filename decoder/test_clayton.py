import io
import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Lock
from unittest.mock import Mock, patch

from asammdf import Signal
from botocore.response import StreamingBody

from decoder.Clayton.send_clayton import decode_file, ingest
from decoder.sending import send_signal_using_json_lines


class ClaytonTest(unittest.TestCase):
    def test_decode_preserves_numeric_states_and_binds_dbc_to_each_bus(self):
        state = Signal(
            samples=[0, 1],
            timestamps=[0, 1],
            name="State",
            conversion={
                "val_0": 0,
                "text_0": b"Off",
                "val_1": 1,
                "text_1": b"On",
            },
            display_names={"CAN1.BMS.State": "", "BMS.State": ""},
        )
        with patch("decoder.Clayton.send_clayton.MDF") as mdf, patch(
            "decoder.Clayton.send_clayton.send_signal_using_json_lines",
            return_value=2,
        ) as send:
            raw = mdf.return_value.__enter__.return_value
            decoded = (
                raw.extract_bus_logging.return_value.__enter__.return_value
            )
            decoded.iter_channels.return_value = [state]
            self.assertEqual(
                decode_file(
                    Path("test.MF4"), "LB101", "http://vm", Path("dbc")
                ),
                2,
            )
            raw.extract_bus_logging.assert_called_once_with(
                database_files={
                    "CAN": [
                        (Path("dbc/loco_CAN500.dbc"), 1),
                        (Path("dbc/loco_CAN250.dbc"), 2),
                    ]
                },
                ignore_value2text_conversion=True,
            )
            self.assertEqual(list(send.call_args.args[0].samples), [0, 1])
            self.assertEqual(
                send.call_args.kwargs["extra_labels"], {"bus": "CAN1"}
            )
            send.side_effect = RuntimeError("HTTP failure")
            with self.assertRaisesRegex(RuntimeError, "HTTP failure"):
                decode_file(Path("test.MF4"), "LB101", "http://vm", Path("dbc"))

    def test_decoded_signal_batches_use_ten_workers(self):
        state = Signal(
            samples=[0, 1],
            timestamps=[0, 1],
            name="State",
            display_names={"CAN1.BMS.State": "", "BMS.State": ""},
        )
        barrier = Barrier(10, timeout=5)
        lock = Lock()
        active = peak = 0

        def send(*args, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            with lock:
                active -= 1
            return 2

        with patch("decoder.Clayton.send_clayton.MDF") as mdf, patch(
            "decoder.Clayton.send_clayton.send_signal_using_json_lines",
            side_effect=send,
        ):
            raw = mdf.return_value.__enter__.return_value
            decoded = (
                raw.extract_bus_logging.return_value.__enter__.return_value
            )
            decoded.iter_channels.return_value = [state] * 20
            self.assertEqual(
                decode_file(
                    Path("test.MF4"), "LB101", "http://vm", Path("dbc")
                ),
                40,
            )
        self.assertEqual(peak, 10)

    def test_checkpoint_resume_replacement_and_late_upload(self):
        obj = {
            "Key": "54988442/log.MF4",
            "ETag": '"revision1"',
            "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
        }
        s3 = Mock()
        s3.get_paginator.return_value.paginate.return_value = [
            {"Contents": [obj]}
        ]
        s3.get_object.side_effect = lambda **_: {
            "Body": StreamingBody(io.BytesIO(b"raw"), 3)
        }
        with sqlite3.connect(":memory:") as state, patch(
            "decoder.Clayton.send_clayton.decode_file", return_value=42
        ) as decode:
            args = (s3, state, {"LB101": "54988442"}, "http://vm", Path("."), 1)
            self.assertEqual(ingest(*args), 42)
            self.assertEqual(ingest(*args), 0)
            obj["ETag"] = '"revision2"'
            self.assertEqual(ingest(*args), 42)
            obj["Key"] = "54988442/late-old-recording.mf4"
            self.assertEqual(ingest(*args), 42)
            self.assertEqual(decode.call_count, 3)
            s3.get_object.assert_called_with(
                Bucket="fvt-telematics", Key=obj["Key"], IfMatch='"revision2"'
            )

    def test_failed_import_is_retried_without_blocking_other_device(self):
        s3 = Mock()
        s3.get_paginator.return_value.paginate.side_effect = lambda **kw: [
            {
                "Contents": [
                    {
                        "Key": kw["Prefix"] + name,
                        "ETag": '"etag"',
                        "LastModified": datetime(
                            2026, 1, 1, tzinfo=timezone.utc
                        ),
                    }
                    for name in (
                        ["z-bad.MF4", "good.MF4"]
                        if kw["Prefix"] == "54988442/"
                        else ["good.MF4"]
                    )
                ]
            }
        ]
        s3.get_object.side_effect = lambda **_: {
            "Body": StreamingBody(io.BytesIO(b"raw"), 3)
        }
        with sqlite3.connect(":memory:") as state, patch(
            "decoder.Clayton.send_clayton.decode_file",
            side_effect=[RuntimeError("HTTP failure"), 23, 24, 42],
        ):
            args = (
                s3,
                state,
                {"LB101": "54988442", "LB102": "B8B864CC"},
                "http://vm",
                Path("."),
                2,
            )
            with self.assertLogs(level="ERROR"), self.assertRaises(
                RuntimeError
            ):
                ingest(*args)
            self.assertEqual(
                state.execute(
                    "SELECT object_key FROM imported ORDER BY object_key"
                ).fetchall(),
                [("54988442/good.MF4",), ("B8B864CC/good.MF4",)],
            )
            self.assertEqual(ingest(*args), 42)

    @patch("decoder.sending.time.sleep")
    @patch("decoder.sending.requests.post")
    def test_all_samples_identity_timestamps_and_failed_http(self, post, sleep):
        signal = Signal(
            samples=[1, 2, 3],
            timestamps=[0, 0.1, 0.2],
            name="Voltage",
            unit="V",
            display_names={
                "CAN1.BMS.Voltage": "display",
                "BMS.Voltage": "display",
            },
        )
        args = dict(
            signal=signal,
            start_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
            job="LB101",
            server="http://vm",
            skip_signal_range_check=True,
            batch_size=2,
            raise_on_error=True,
            extra_labels={"bus": "CAN1"},
        )
        post.return_value.status_code = 204
        self.assertEqual(send_signal_using_json_lines(**args), 3)
        lines = [json.loads(c.kwargs["data"]) for c in post.call_args_list]
        self.assertEqual(
            [v for line in lines for v in line["values"]], [1, 2, 3]
        )
        self.assertEqual(
            [t for line in lines for t in line["timestamps"]],
            [1767225600000, 1767225600100, 1767225600200],
        )
        self.assertEqual(
            lines[0]["metric"],
            {
                "__name__": "Voltage",
                "job": "LB101",
                "message": "BMS",
                "unit": "V",
                "bus": "CAN1",
            },
        )
        post.return_value.status_code = 500
        with self.assertLogs("send_signal_using_json_lines", level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, "imported 0/3"):
                send_signal_using_json_lines(**args)


if __name__ == "__main__":
    unittest.main()
