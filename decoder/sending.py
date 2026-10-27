import time
import json
import os
import sys
import logging
from pathlib import Path

import requests
from asammdf import MDF, Signal
from asammdf.blocks.types import DbcFileType, BusType
from datetime import datetime, timedelta
from collections.abc import Iterable
from typing import Sequence, Callable, Optional, Any, cast
from concurrent.futures import ThreadPoolExecutor, as_completed

if __name__ == "__main__":
    sys.path.append(str(Path(__file__).parent.parent))

from decoder.config import *
from decoder.utils import *
from decoder.livelogger.CANReader import CANReader
from decoder.livelogger.DBCDecoder import DBCDecoder


def normalize_dbc_entries(entries: Iterable[DbcFileType | Any]) -> list[DbcFileType]:
    normalized: list[DbcFileType] = []
    for entry in entries:
        if (
            isinstance(entry, tuple)
            and len(entry) == 2
            and isinstance(entry[1], int)
        ):
            normalized.append(cast(DbcFileType, entry))
        else:
            normalized.append((cast(Any, entry), 0))
    return normalized


def _extend_no_proxy(entries: list[str]) -> None:
    # Preserve existing NO_PROXY/no_proxy values and append entries needed for local/tailnet VM access.
    existing_raw = (
        os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    )
    existing = [e.strip() for e in existing_raw.split(",") if e.strip()]
    merged: list[str] = []

    for value in [*existing, *entries]:
        if value and value not in merged:
            merged.append(value)

    no_proxy_value = ",".join(merged)
    os.environ["NO_PROXY"] = no_proxy_value
    os.environ["no_proxy"] = no_proxy_value


_extend_no_proxy(
    [
        "localhost",
        "127.0.0.1",
        "::1",
        ".ts.net",
        ".tailnet",
    ]
)


def get_mf4_files(
    directory: Path | str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[Path]:
    """
    Get all MDF4 files in the specified directory.
    If start_date is provided, only return files modified after that datetime.
    If end_date is provided, only return files modified before that datetime.
    """
    if not isinstance(directory, Path):
        directory = Path(directory)

    files = list(directory.rglob("*.[mM][fF]4"))
    if start_date is not None:
        files = [
            f
            for f in files
            if datetime.fromtimestamp(f.stat().st_mtime, tz=start_date.tzinfo)
            > start_date
        ]

    if end_date is not None:
        files = [
            f
            for f in files
            if datetime.fromtimestamp(f.stat().st_mtime, tz=end_date.tzinfo)
            < end_date
        ]
    return files


def get_dbc_dict(directory: Path | str) -> dict[BusType, Iterable[DbcFileType]]:
    """
    Get a dictionary of DBC files in the specified directory.
    This dictionary can be passed directly to extract_bus_logging() in asammdf.
    """

    dbc_files = get_dbc_files(directory)
    return {"CAN": [(file, 0) for file in dbc_files]}


def get_channel_data(signal: Signal) -> tuple[str, str]:
    display_names = list(signal.display_names.keys())
    message = display_names[1].split(".")[0]
    name = signal.name.replace(" ", "_")
    return message, name


def is_valid_sample(sample):
    """Check if sample can be converted to a numeric value"""
    try:
        float(sample)  # Try converting to float
        return True
    except (ValueError, TypeError):
        return False


def check_signal_range(
    signal: Signal, start_time: datetime, server: str
) -> Signal | None:
    """
    Checks if the signal timestamps already exist in the database, returns a Signal object only with timestamps not already there,
    """

    logger = logging.getLogger("check_signal_range")
    setup_simple_logger(logger, format=LOG_FORMAT)

    # Query VictoriaMetrics to check if data for this signal exists in the given time range
    message, metric_name = get_channel_data(signal)
    start_ts = (
        start_time + timedelta(seconds=signal.timestamps[0])
    ).timestamp()
    end_ts = (start_time + timedelta(seconds=signal.timestamps[-1])).timestamp()

    params: dict[str, str] = {
        "match[]": f'{metric_name}{{message="{message}"}}',
        "start": str(start_ts),
        "end": str(end_ts),
        "step": "1s",
    }
    try:
        resp = requests.get(server + vmapi_export, params=params, timeout=10)
        if resp.status_code != 200:
            return signal
        elif resp.text == "":
            return signal
        else:
            # Data exists for this signal in the range
            # Cut the data that already exists
            _json = json.loads(resp.text)
            respstart_ts = datetime.fromtimestamp(
                1e-3 * _json["timestamps"][0], tz=start_time.tzinfo
            )
            respend_ts = datetime.fromtimestamp(
                1e-3 * _json["timestamps"][-1], tz=start_time.tzinfo
            )

            cutstart = (respstart_ts - start_time).total_seconds()
            cutend = (respend_ts - start_time).total_seconds()

            eps = 1e-3  # Adjusts for precision, acceptable to lose 1ms of data
            older_data = signal.cut(signal.timestamps[0] + eps, cutstart - eps)
            newer_data = signal.cut(cutend + eps, signal.timestamps[-1] - eps)

            newsig = older_data.extend(newer_data)
            return newsig if len(newsig.timestamps) > 0 else None
    except Exception as e:
        logger.warning(
            f"⚠️ Warning: Could not check Signal range for {metric_name}: {e}"
        )
    return signal


def _resolve_job_watermark(
    server: str, job: str, skip_signal_range_check: bool
) -> datetime | None:
    if skip_signal_range_check or not job:
        return None

    result = get_latest_vm_job_timestamp(server=server, job=job)
    watermark = result.get("timestamp")
    if result.get("has_data") and isinstance(watermark, datetime):
        return watermark
    return None


def _apply_job_watermark(
    signal: Signal,
    start_time: datetime,
    job_watermark: datetime | None,
) -> Signal | None:
    if job_watermark is None or len(signal.timestamps) == 0:
        return signal

    signal_start = start_time + timedelta(seconds=signal.timestamps[0])
    signal_end = start_time + timedelta(seconds=signal.timestamps[-1])

    if signal_end <= job_watermark:
        return None

    if signal_start >= job_watermark:
        return signal

    cutoff = (job_watermark - start_time).total_seconds()
    first_sample_idx = next(
        (
            idx
            for idx, timestamp in enumerate(signal.timestamps)
            if timestamp > cutoff
        ),
        None,
    )
    if first_sample_idx is None:
        return None

    trimmed = signal.cut(
        signal.timestamps[first_sample_idx], signal.timestamps[-1]
    )
    return trimmed if len(trimmed.timestamps) > 0 else None


def send_signal(
    signal: Signal,
    start_time: datetime,
    job: str,
    server: str,
    print_metric_line: bool = False,
    send_signal: bool = True,
    skip_signal_range_check: bool = False,
    job_watermark: datetime | None = None,
    batch_size: int = 250_000,
) -> int:
    """
    Send a single signal to VictoriaMetrics.
    Options:
    - signal: The Signal object to send.
    - start_time: The datetime representing the start time of the MDF file.
    - job: Label to recognize what machine this signal belongs to.
    - print_metric_line: If True, prints the metric lines before sending (default: False).
    - send_signal: If True, actually sends the data to VictoriaMetrics (default: True).
    - skip_signal_range_check: If True, skips checking if the signal data already exists in the database (default: False).
    - batch_size: Number of samples to send in each HTTP POST batch (default: 250,000).
    """

    logger = logging.getLogger("send_signal")
    setup_simple_logger(logger)

    message, metric_name = get_channel_data(signal)

    num_of_samples_sent = 0

    if job_watermark is None and not skip_signal_range_check:
        job_watermark = _resolve_job_watermark(
            server=server, job=job, skip_signal_range_check=skip_signal_range_check
        )

    _signal: Signal | None = signal
    if not skip_signal_range_check:
        _signal = _apply_job_watermark(signal, start_time, job_watermark)

    if _signal is None or len(_signal.timestamps) < 1:
        logger.info(f"  ☑️ No new data for {signal.name}, skipping ...")
        return num_of_samples_sent

    unit = _signal.unit if _signal.unit else ""
    _sig_start_str = start_time + timedelta(seconds=_signal.timestamps[0])
    _sig_end_str = start_time + timedelta(seconds=_signal.timestamps[-1])
    _time_str = f"{_sig_start_str.isoformat()} - {_sig_end_str.isoformat()}, {len(_signal.timestamps)} samples"

    logger.debug(f"  📨 Sending {metric_name} [{_time_str}] ...")
    start = time.time()
    batch: list[str] = []
    for sample, ts in zip(_signal.samples, _signal.timestamps):
        if not is_valid_sample(
            sample
        ):  # Check if sample is not float (e.g. string)
            continue  # Skip this sample
        data = make_metric_line(
            metric_name,
            message,
            unit,
            sample,
            start_time + timedelta(seconds=ts),
            job=job if job else "",
        )
        batch.append(data)
        num_of_samples_sent += 1
        if len(batch) >= batch_size:
            try:
                if print_metric_line:
                    logger.info("".join(batch))
                if send_signal:
                    requests.post(
                        server + vmapi_import_prometheus, data="".join(batch)
                    )
            except Exception as e:
                logger.error(f"‼️ Error sending batch: {e}")
            batch = []
            time.sleep(0.01)  # Avoid overwhelming the server
    if batch:
        try:
            requests.post(server + vmapi_import_prometheus, data="".join(batch))
        except Exception as e:
            logger.error(f"‼️ Error sending final batch: {e}")

    time_str = get_time_str(start)
    end_ts = time.time()
    logger.info(
        f"  📨 Sent {metric_name} in {time_str} ({convert_to_eng(num_of_samples_sent)} samples | {convert_to_eng(num_of_samples_sent / (end_ts - start))} samples/s)"
    )
    return num_of_samples_sent


def send_signal_using_json_lines(
    signal: Signal,
    start_time: datetime,
    job: str,
    server: str,
    print_metric_line: bool = False,
    send_signal: bool = True,
    skip_signal_range_check: bool = False,
    job_watermark: datetime | None = None,
    batch_size: int = 10_000,
    raise_on_error: bool = False,
    extra_labels: dict[str, str] | None = None,
) -> int:
    """
    Send a single signal to VictoriaMetrics using JSON lines.
    Options:
    - signal: The Signal object to send.
    - start_time: The datetime representing the start time of the MDF file.
    - job: Label to recognize what machine this signal belongs to.
    - print_metric_line: If True, prints the metric lines before sending (default: False).
    - send_signal: If True, actually sends the data to VictoriaMetrics (default: True).
    - skip_signal_range_check: If True, skips checking if the signal data already exists in the database (default: False).
    - batch_size: Number of samples to send in each HTTP POST batch (default: 10,000) see https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/#json-line-format.
    """

    logger = logging.getLogger("send_signal_using_json_lines")
    setup_simple_logger(logger)

    message, metric_name = get_channel_data(signal)

    num_of_samples_sent = 0

    if job_watermark is None and not skip_signal_range_check:
        job_watermark = _resolve_job_watermark(
            server=server, job=job, skip_signal_range_check=skip_signal_range_check
        )

    _signal: Signal | None = signal
    if not skip_signal_range_check:
        _signal = _apply_job_watermark(signal, start_time, job_watermark)

    if _signal is None or len(_signal.timestamps) < 1:
        logger.debug(f"  ☑️ No new data for {metric_name}, skipping ...")
        return num_of_samples_sent

    unit = _signal.unit if _signal.unit else ""
    _sig_start_str = start_time + timedelta(seconds=_signal.timestamps[0])
    _sig_end_str = start_time + timedelta(seconds=_signal.timestamps[-1])
    values: list[float] = []
    timestamps: list[int] = []
    for sample, ts in zip(_signal.samples, _signal.timestamps):
        if not is_valid_sample(
            sample
        ):  # Check if sample is not float (e.g. string)
            continue  # Skip this sample
        values.append(float(sample))
        timestamps.append(
            int((start_time + timedelta(seconds=ts)).timestamp() * 1e3)
        )
    _time_str = f"{_sig_start_str.isoformat()} - {_sig_end_str.isoformat()}, {len(timestamps)} samples"

    if len(values) < 1 or len(timestamps) < 1:
        logger.debug(
            f"  ℹ️ No valid numeric data for {metric_name}, skipping ..."
        )
        return num_of_samples_sent

    logger.debug(f"  📨 Sending {metric_name} [{_time_str}] ...")
    lines, counts = make_list_of_vm_json_line_format(
        metric_name=metric_name,
        message=message,
        unit=unit,
        values=values,
        timestamps_in_ms=timestamps,
        job=job if job else "",
        batch_size=batch_size,
        extra_labels=extra_labels,
    )

    start = time.time()
    for line, count in zip(lines, counts):
        try:
            if print_metric_line:
                logger.info(line)
            if send_signal:
                for retries in range(5):  # Retry up to 5 times
                    try:
                        resp = requests.post(
                            server + vmapi_import,
                            data=line,
                            timeout=10,
                        )
                        time.sleep(0.01)  # Avoid overwhelming the server
                        if resp.status_code == 204:
                            num_of_samples_sent += count
                            break  # Success, exit retry loop
                        else:
                            logger.error(
                                f"‼️ {metric_name}: Error sending batch (attempt {retries + 1}): HTTP {resp.status_code} - {resp.text}"
                            )
                    except requests.RequestException as e:
                        logger.error(
                            f"‼️ {metric_name}: Exception sending batch (attempt {retries + 1}): {e}"
                        )
                    time.sleep(2**retries)  # Exponential backoff

        except Exception as e:
            logger.error(f"‼️ {metric_name}: Error sending batch: {e}")

    if raise_on_error and send_signal and num_of_samples_sent != len(values):
        raise RuntimeError(
            f"{metric_name}: imported {num_of_samples_sent}/{len(values)} samples"
        )

    if num_of_samples_sent == 0:
        logger.warning(f"  ⚠️ {metric_name}: No samples sent.")
    else:
        time_str = get_time_str(start)
        end_ts = time.time()
        logger.info(
            f"  📨 Sent {metric_name} in {time_str} ({convert_to_eng(num_of_samples_sent)} samples | {convert_to_eng(num_of_samples_sent / (end_ts - start))} samples/s)"
        )
    return num_of_samples_sent


def send_file(
    filename: Path,
    server: str,
    job: str | None = None,
    skip_signal_range_check: bool = True,
    skip_signal_fn: Optional[Callable[[str], bool]] = None,
    batch_size: int = 10_000,
    max_thread_workers=10,
    job_watermark: datetime | None = None,
) -> dict[str, int]:
    logger = logging.getLogger("send_file")
    setup_simple_logger(logger, format=LOG_FORMAT)

    logger.debug(f"Sending {filename}")
    signals_sample_count: dict[str, int] = {}
    if not filename.exists():
        logger.warning(f"📃 File {filename} does not exist.")
        return signals_sample_count

    if not filename.is_file():
        logger.warning(f"📃 {filename} is not a file.")
        return signals_sample_count

    if not filename.suffix.lower() == ".mf4":
        logger.warning(f"📃 {filename} is not a valid MDF4 file.")
        return signals_sample_count

    try:
        resolved_job = job if job else filename.stem
        if job_watermark is None and not skip_signal_range_check:
            job_watermark = _resolve_job_watermark(
                server=server,
                job=resolved_job,
                skip_signal_range_check=skip_signal_range_check,
            )
        with MDF(filename) as mdf:
            with ThreadPoolExecutor(max_workers=max_thread_workers) as executor:
                future_to_signal = {
                    executor.submit(
                        send_signal_using_json_lines,
                        signal=sig,
                        start_time=mdf.start_time,
                        job=resolved_job,
                        skip_signal_range_check=skip_signal_range_check,
                        job_watermark=job_watermark,
                        batch_size=batch_size,
                        server=server,
                    ): sig
                    for sig in mdf.iter_channels()
                    if skip_signal_fn is None or not skip_signal_fn(sig.name)
                }

                for future in as_completed(future_to_signal):
                    sig = future_to_signal[future]
                    try:
                        samples_sent = future.result()
                        if samples_sent > 0:
                            if sig.name in signals_sample_count:
                                signals_sample_count[sig.name] += samples_sent
                            else:
                                signals_sample_count[sig.name] = samples_sent
                    except Exception as e:
                        logger.error(f"❌ Error sending signal {sig.name}: {e}")

    except Exception as e:
        logger.error(f"❌ Error processing {filename}: {e}")

    return signals_sample_count


def send_decoded(
    decoded: Path | MDF,
    server: str,
    job: str | None = None,
    skip_signal_range_check: bool = True,
    skip_signal_fn: Optional[Callable[[str], bool]] = None,
    batch_size: int = 10_000,
    max_thread_workers: int = 10,
    job_watermark: datetime | None = None,
) -> dict[str, int]:
    """
    Send a decoded MDF4 file to VictoriaMetrics.
    """
    logger = logging.getLogger("send_decoded")
    setup_simple_logger(logger, format=LOG_FORMAT)

    signals_sample_count: dict[str, int] = {}

    if isinstance(decoded, Path):
        return send_file(
            filename=decoded,
            job=job,
            server=server,
            skip_signal_range_check=skip_signal_range_check,
            skip_signal_fn=skip_signal_fn,
            batch_size=batch_size,
            max_thread_workers=max_thread_workers,
            job_watermark=job_watermark,
        )
    elif isinstance(decoded, MDF):
        resolved_job = job if job else "-".join(decoded.name.parts)
        if job_watermark is None and not skip_signal_range_check:
            job_watermark = _resolve_job_watermark(
                server=server,
                job=resolved_job,
                skip_signal_range_check=skip_signal_range_check,
            )
        with ThreadPoolExecutor(max_workers=max_thread_workers) as executor:
            future_to_signal = {
                executor.submit(
                    send_signal_using_json_lines,
                    signal=sig,
                    start_time=decoded.start_time,
                    job=resolved_job,
                    skip_signal_range_check=skip_signal_range_check,
                    job_watermark=job_watermark,
                    batch_size=batch_size,
                    server=server,
                ): sig
                for sig in decoded.iter_channels()
                if skip_signal_fn is None or not skip_signal_fn(sig.name)
            }

            for future in as_completed(future_to_signal):
                sig = future_to_signal[future]
                try:
                    samples_sent = future.result()
                    if samples_sent > 0:
                        if sig.name in signals_sample_count:
                            signals_sample_count[sig.name] += samples_sent
                        else:
                            signals_sample_count[sig.name] = samples_sent
                except Exception as e:
                    logger.error(f"❌ Error sending signal {sig.name}: {e}")
    else:
        logger.warning(
            "⚠️ Invalid decoded input type. Must be Path or MDF instance."
        )

    return signals_sample_count


def decode_and_send(
    files: list[Path],
    dbc_files: Sequence[DbcFileType],
    server: str,
    job: str = "test_job",
    stack_first: bool = True,
    stack_msg: str = "Stack",
    skip_signal_range_check: bool = True,
    skip_signal_fn: Optional[Callable[[str], bool]] = None,
    batch_size=10_000,
) -> dict[str, int]:
    """
    Decode all MDF4 files in the specified directory and send their data to VictoriaMetrics.
    """
    logger = logging.getLogger("decode_and_send")
    setup_simple_logger(logger, format=LOG_FORMAT)

    signals_sample_count: dict[str, int] = {}

    if not files:
        logger.warning("⚠️ No directory or files specified.")
        return signals_sample_count

    database_files: dict[BusType, Iterable[DbcFileType]] = {
        "CAN": normalize_dbc_entries(dbc_files)
    }

    if not dbc_files:
        logger.error("⚠️ No DBC files specified.")
        return signals_sample_count

    if stack_first and len(files) > 1:
        mdf = MDF()
        try:
            logger.info(f" ⏳ {stack_msg}: Stacking {len(files)} files")
            start = time.time()
            mdf = MDF().stack(files)
            logger.info(f" ✅ {stack_msg}: Stacked in {get_time_str(start)}s")

            try:
                logger.info(f" ⏳ {stack_msg}: Decoding stacked file")
                start = time.time()
                decoded = mdf.extract_bus_logging(
                    database_files, ignore_value2text_conversion=True
                )
                logger.info(
                    f" ✅ {stack_msg}: Decoded in {get_time_str(start)}s"
                )
                if list(decoded.iter_channels()):
                    result = send_decoded(
                        decoded=decoded,
                        job=job,
                        skip_signal_fn=skip_signal_fn,
                        skip_signal_range_check=skip_signal_range_check,
                        batch_size=batch_size,
                        server=server,
                    )
                    for k, v in result.items():
                        signals_sample_count[k] = (
                            signals_sample_count.get(k, 0) + v
                        )
                else:
                    logger.warning("⚠️ No signals found, skipping sending.")

            except Exception as e:
                logger.error(f"❌ Error decoding stacking files: {e}")
        except Exception as e:
            logger.error(f"❌ Error stacking files: {e}")

    else:
        for file in files:
            mdf = MDF(file)
            if job is None:
                job = file.stem
            _dispname = (
                "../" + "/".join(file.parts[-2:])
                if len(file.parts) >= 2
                else str(file.as_posix())
            )
            try:
                start = time.time()
                logger.info(f" ⏳ Decoding ../{_dispname} ...")
                decoded = mdf.extract_bus_logging(
                    database_files, ignore_value2text_conversion=True
                )
                logger.info(
                    f" ✅ Decoded ../{_dispname} in {time.time() - start:.3f}s"
                )
                if list(decoded.iter_channels()):
                    result = send_decoded(
                        decoded=decoded,
                        job=job,
                        skip_signal_fn=skip_signal_fn,
                        skip_signal_range_check=skip_signal_range_check,
                        batch_size=batch_size,
                        server=server,
                    )

                    for k, v in result.items():
                        signals_sample_count[k] = (
                            signals_sample_count.get(k, 0) + v
                        )
                else:
                    logger.warning(
                        f"⚠️ No signals found in {_dispname}, skipping sending."
                    )
            except Exception as e:
                logger.error(f"❌ Error decoding {_dispname}: {e}")
                continue

    return signals_sample_count


if __name__ == "__main__":
    # main_livestream()
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.info("Sending module called directly, no action taken.")
