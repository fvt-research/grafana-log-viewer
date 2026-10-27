"""Import all recorded Clayton battery signals, resuming per S3 object."""

import argparse
import json
import logging
import sqlite3
import tempfile
from pathlib import Path

from asammdf import MDF

from decoder.config import LOG_FORMAT, server_vm_clayton
from decoder.s3_helper import create_s3_client
from decoder.sending import send_signal_using_json_lines
from decoder.utils import install_verbosity_level

ASSETS = Path(__file__).resolve().parent
BUCKET = "fvt-telematics"


def decode_file(path: Path, job: str, server: str, dbc_folder: Path) -> int:
    samples = 0
    with MDF(path) as raw:
        with raw.extract_bus_logging(
            database_files={
                "CAN": [
                    (dbc_folder / "loco_CAN500.dbc", 1),
                    (dbc_folder / "loco_CAN250.dbc", 2),
                ]
            },
            ignore_value2text_conversion=True,
        ) as decoded:
            for signal in decoded.iter_channels(raw=True):
                signal = signal.physical(ignore_value2text_conversions=True)
                samples += send_signal_using_json_lines(
                    signal,
                    start_time=decoded.start_time,
                    job=job,
                    server=server,
                    skip_signal_range_check=True,
                    raise_on_error=True,
                    extra_labels={
                        "bus": next(iter(signal.display_names)).split(".")[0]
                    },
                )
    if not samples:
        raise RuntimeError(f"No decoded samples in {path.name} for {job}")
    return samples


def ingest(
    s3,
    state: sqlite3.Connection,
    devices: dict[str, str],
    server: str,
    dbc_folder: Path,
    max_files_per_device: int,
) -> int:
    state.execute("""CREATE TABLE IF NOT EXISTS imported (
        bucket TEXT NOT NULL, object_key TEXT NOT NULL, etag TEXT NOT NULL,
        samples INTEGER NOT NULL,
        PRIMARY KEY (bucket, object_key))""")
    total = 0
    failed = False
    # ponytail: one file at a time bounds memory; parallelize only after profiling.
    with tempfile.TemporaryDirectory(prefix="clayton-") as directory:
        local = Path(directory) / "raw.MF4"
        for job, device in devices.items():
            try:
                pending = []
                for page in s3.get_paginator("list_objects_v2").paginate(
                    Bucket=BUCKET, Prefix=f"{device}/"
                ):
                    for obj in page.get("Contents", []):
                        if not obj["Key"].lower().endswith(".mf4"):
                            continue
                        previous = state.execute(
                            "SELECT etag FROM imported "
                            "WHERE bucket = ? AND object_key = ?",
                            (BUCKET, obj["Key"]),
                        ).fetchone()
                        if previous is None or previous[0] != obj["ETag"]:
                            pending.append(obj)
                pending.sort(
                    key=lambda obj: (obj["LastModified"], obj["Key"]),
                    reverse=True,
                )
                logging.info("%s: %d files pending", job, len(pending))
            except Exception:
                logging.exception("%s: S3 listing failed", job)
                failed = True
                continue
            for obj in pending[:max_files_per_device]:
                key = obj["Key"]
                logging.info("%s: decoding s3://%s/%s", job, BUCKET, key)
                try:
                    # Pin the download to the listed revision before checkpointing.
                    response = s3.get_object(
                        Bucket=BUCKET, Key=key, IfMatch=obj["ETag"]
                    )
                    with response["Body"] as body, local.open("wb") as target:
                        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                            target.write(chunk)
                    count = decode_file(local, job, server, dbc_folder)
                    with state:
                        state.execute(
                            "INSERT OR REPLACE INTO imported VALUES (?, ?, ?, ?)",
                            (BUCKET, key, obj["ETag"], count),
                        )
                    local.unlink()
                    total += count
                    logging.info(
                        "%s: imported %d samples from %s", job, count, key
                    )
                except Exception:
                    logging.exception(
                        "%s: %s failed; file not checkpointed", job, key
                    )
                    failed = True
    if failed:
        raise RuntimeError(
            "Clayton ingestion incomplete; failed files will retry"
        )
    return total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=server_vm_clayton)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument(
        "--devices-file", type=Path, default=ASSETS / "devices.json"
    )
    parser.add_argument("--dbc-folder", type=Path, default=ASSETS / "dbc")
    parser.add_argument("--max-files-per-device", type=int, default=10)
    args = parser.parse_args()
    if args.max_files_per_device < 1:
        parser.error("--max-files-per-device must be positive")
    devices = json.loads(args.devices_file.read_text(encoding="utf-8"))
    if not isinstance(devices, dict) or not devices:
        parser.error(
            "--devices-file must contain a nonempty job-to-device object"
        )
    if any(
        not isinstance(job, str)
        or not job.strip()
        or not isinstance(device, str)
        or len(device) != 8
        or any(c not in "0123456789ABCDEF" for c in device)
        for job, device in devices.items()
    ) or len(set(devices.values())) != len(devices):
        parser.error(
            "Devices require distinct eight-character uppercase hex IDs"
        )
    for name in ("loco_CAN250.dbc", "loco_CAN500.dbc"):
        if not (args.dbc_folder / name).is_file():
            parser.error(f"Missing DBC: {args.dbc_folder / name}")
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    install_verbosity_level("minimal")
    logging.getLogger().setLevel(logging.INFO)
    args.state_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(args.state_file) as state:
        count = ingest(
            create_s3_client(),
            state,
            devices,
            args.server.rstrip("/"),
            args.dbc_folder,
            args.max_files_per_device,
        )
    logging.info("Clayton: imported %d samples", count)


if __name__ == "__main__":
    main()
