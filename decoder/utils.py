import time
import os
import sys
import subprocess
import logging
import subprocess
import shutil
from pathlib import Path

from datetime import datetime, timedelta, timezone
from asammdf import MDF
from asammdf.blocks.v4_blocks import HeaderBlock
from asammdf.blocks.types import StrPath
import requests
import json
import re
from itertools import chain
from can import LogReader, Logger
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any

if __name__ == "__main__":
    sys.path.append(str(Path(__file__).parent.parent))

from decoder.config import *


SUMMARY: int = 25
"""Per-device / per-batch summary level (shown at some/minimal, hidden at silent)."""
FINAL_SUMMARY: int = 35
"""Final overall run total level (shown at all levels including silent)."""
logging.addLevelName(SUMMARY, "SUMMARY")
logging.addLevelName(FINAL_SUMMARY, "FINAL_SUMMARY")

_cli_log_level: int | None = None


def install_verbosity_level(verbosity: str) -> None:
    """Set the effective log level from a --verbosity argument.

    debug    → DEBUG   (everything)
    some     → INFO    (hides per-signal Sending... and S3 scan/discovery)
    minimal  → SUMMARY (also hides per-signal Sent... and per-file streamed)
    silent   → WARNING (hides everything except FINAL_SUMMARY and errors)
    """
    global _cli_log_level
    level_map = {
        "debug": logging.DEBUG,
        "some": logging.INFO,
        "minimal": SUMMARY,
        "silent": logging.WARNING,
    }
    _cli_log_level = level_map.get(verbosity, logging.DEBUG)
    logging.getLogger().setLevel(_cli_log_level)

    aws_level = logging.DEBUG if _cli_log_level == logging.DEBUG else logging.WARNING
    for name in ("boto3", "botocore", "s3transfer", "urllib3"):
        logging.getLogger(name).setLevel(aws_level)


def log_summary(msg: object, *args: object, **kwargs: object) -> None:
    """Log at SUMMARY level (per-device/batch totals)."""
    logging.log(SUMMARY, msg, *args, **kwargs)


def log_final(msg: object, *args: object, **kwargs: object) -> None:
    """Log at FINAL_SUMMARY level (overall run totals; shown even in silent)."""
    logging.log(FINAL_SUMMARY, msg, *args, **kwargs)


def setup_simple_logger(
    logger: logging.Logger,
    level: int = logging.INFO,
    format: str = "%(message)s",
):
    # Define a simple logger
    handler = logging.StreamHandler()
    formatter = logging.Formatter(format)
    handler.setFormatter(formatter)
    if not logger.hasHandlers():
        logger.addHandler(handler)
    logger.propagate = False
    logger.setLevel(_cli_log_level if _cli_log_level is not None else level)


def get_time_str(start_time: float, end_ts: float | None = None) -> str:
    _end_ts = time.time() if end_ts is None else end_ts
    elapsed = _end_ts - start_time
    return format_duration_seconds(elapsed)


def format_duration_seconds(elapsed: float) -> str:
    days, rem = divmod(elapsed, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)

    parts = []
    if days:
        parts.append(f"{int(days)}d")
    if hours or parts:
        parts.append(f"{int(hours)}h")
    if mins or parts:
        parts.append(f"{int(mins)}m")
    if secs or parts:
        parts.append(f"{secs:.3f}s")

    return "".join(parts) or "0s"


def format_time_span(start: datetime, end: datetime) -> str:
    return (
        f"{start.isoformat()} - {end.isoformat()} "
        f"({format_duration_seconds((end - start).total_seconds())})"
    )


def format_bytes(value: int | None) -> str:
    if value is None:
        return "n/a"
    size = float(value)
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit_idx = 0
    while size >= 1024.0 and unit_idx < len(units) - 1:
        size /= 1024.0
        unit_idx += 1
    if unit_idx == 0:
        return f"{int(size)}{units[unit_idx]}"
    return f"{size:.3f}{units[unit_idx]}"


def parse_time_arg(value: str, now: datetime, allow_today: bool = True) -> datetime:
    value = value.strip()
    if value == "now":
        return now
    if allow_today and value == "today":
        return datetime.today().astimezone(now.tzinfo)

    offset_match = re.fullmatch(r"(\d+)([smhd])", value)
    if offset_match:
        amount, unit = offset_match.groups()
        delta = {
            "s": timedelta(seconds=int(amount)),
            "m": timedelta(minutes=int(amount)),
            "h": timedelta(hours=int(amount)),
            "d": timedelta(days=int(amount)),
        }[unit]
        return now - delta

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=now.tzinfo)
    return parsed.astimezone(now.tzinfo)


def get_files(
    directory: Path | str, extension: str | list[str]
) -> list[StrPath]:
    """
    Get all files with the specified extensions in the specified directory.
    """

    if not isinstance(directory, Path):
        directory = Path(directory)

    patterns: list[str] = []

    if isinstance(extension, str):
        patterns = (
            [f"*{extension}"]
            if extension.startswith(".")
            else [f"*.{extension}"]
        )
    elif isinstance(extension, list) and all(
        isinstance(ext, str) for ext in extension
    ):
        patterns = [
            f"*{ext}" if ext.startswith(".") else f"*.{ext}"
            for ext in extension
        ]
    else:
        raise ValueError("Extension must be a string or a list of strings.")

    return list(
        chain.from_iterable(directory.rglob(pattern) for pattern in patterns)
    )


def get_dbc_files(directory: Path | str) -> list[StrPath]:
    """
    Get all DBC files in the specified directory.
    """

    if not isinstance(directory, Path):
        directory = Path(directory)

    return get_files(directory, [".dbc", ".DBC"])


def get_mdf_start_time(mdf: Path) -> datetime | None:
    """
    Get the start time from the MDF file header.
    """

    if not isinstance(mdf, Path):
        mdf = Path(mdf)

    if not mdf.exists():
        logging.info(f" ☹️ File not found: {mdf}")
        return None

    if mdf.suffix.lower() != ".mf4":
        logging.info(f" ☹️ Not a valid MDF file: {mdf}")
        return None

    with open(mdf, "rb") as stream:
        # Read the header block to quickly retrieve the start-time without loading the whole file
        stream.seek(0, 2)  # Seek to end of file
        file_limit = stream.tell()
        stream.seek(0)  # Seek back to start of file
        try:
            header: HeaderBlock = HeaderBlock(
                address=0x40,
                stream=stream,
                mapped=False,
                file_limit=file_limit,
            )
            if header.start_time is None:
                logging.error(f"❌ No start time found in MDF header of {mdf}")
                return None
            return header.start_time
        except Exception as e:
            logging.error(f"❌ Error reading MDF header from {mdf}: {e}")
            return None

    return None


def get_trc_start_time(
    trc: Path, use_iso_line: bool = False
) -> datetime | None:
    """
    Get the start time from the TRC file without having to read the whole file
    """

    if not isinstance(trc, Path):
        trc = Path(trc)

    if not trc.exists():
        logging.info(f" ☹️ File not found: {trc}")

    if trc.suffix.lower() != ".trc":
        logging.info(f" ☹️ Not a valid TRC file: {trc}")

    with open(trc, "r") as stream:
        for line in stream:
            _line = line.strip()
            logging.debug(f"🔍 Reading line: {_line}")

            if use_iso_line:
                # Find the comment that starts with "; Start time:"
                if not line.startswith(";"):
                    break
                match = re.match(r";.+start time: (.+)", _line.lower())
                if not match:
                    continue
                else:
                    try:
                        return datetime.strptime(
                            match.group(1), "%d.%m.%Y %H:%M:%S.%f"
                        )
                    except ValueError as ve:
                        logging.error(
                            f"❌ Invalid start_time format: {match.group(1)}"
                        )
                        return None
            else:
                # Find the comment that starts with ";$STARTTIME="
                if not line.startswith(";$"):
                    break
                if line.startswith(";$STARTTIME="):
                    # Get the offset
                    offset = float(_line.split("=")[1])
                    return datetime(1899, 12, 30) + timedelta(days=offset)

    return None


def make_list_of_vm_json_line_format(
    metric_name: str,
    message: str,
    unit: str,
    values: list[float],
    timestamps_in_ms: list[datetime] | list[int],
    job: str,
    batch_size: int = 10_000,
    extra_labels: dict[str, str] | None = None,
) -> tuple[list[str], list[int]]:
    """
    Create a list of JSON lines in the VictoriaMetrics format for batch uploading.
    The JSON format is as follows:
    {
        "metric": {
            "__name__": "metric_name",
            "job": "job_name",
            "message": "message",
            "unit": "unit"
        },
        "values": [value1, value2, ...],
        "timestamps": [timestamp1, timestamp2, ...]
    }

    The total samples in each JSON line should not exceed the batch_size.

    VictoriaMetrics suggest a max of 1k to 10k samples per batch for optimal performance.
    See: https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/#json-line-format

    Returns a tuple of two lists:
    - List of JSON lines as strings.
    - List of counts of samples in each JSON line.
    """

    if len(values) != len(timestamps_in_ms):
        raise ValueError("Values and timestamps must have the same length.")
    if batch_size <= 0:
        raise ValueError("Batch size must be a positive integer.")

    if len(values) == 0:
        return [], []

    def make_line(start_idx: int, end_idx: int) -> tuple[str, int]:
        json_line = {
            "metric": {
                "__name__": metric_name,
                "job": job.replace(" ", "_"),
                "message": message,
                "unit": unit,
            },
            "values": values[start_idx:end_idx],
            "timestamps": [
                int(ts.timestamp() * 1e3) if isinstance(ts, datetime) else ts
                for ts in timestamps_in_ms[start_idx:end_idx]
            ],
        }
        if extra_labels:
            json_line["metric"].update(extra_labels)
        return json.dumps(json_line), len(values[start_idx:end_idx])

    lines: list[str] = []
    counts: list[int] = []
    for i in range(0, len(values), batch_size):
        line, count = make_line(i, min(i + batch_size, len(values)))
        lines.append(line)
        counts.append(count)

    return lines, counts


def make_metric_line(
    metric_name: str,
    message: str,
    unit: str,
    value: float,
    timestamp: datetime | float,
    job: str = "",
) -> str:
    # Format the metric line for Prometheus
    job_underscored = job.replace(" ", "_")
    return f'{metric_name}{{job="{job_underscored}",message="{message}",unit="{unit}"}} {value} {timestamp.timestamp() if type(timestamp) is datetime else timestamp}\n'


def is_victoriametrics_online(server: str, timeout: float = 3.0) -> bool:
    logger = logging.getLogger("vm_check")
    setup_simple_logger(logger, level=logging.INFO, format=LOG_FORMAT)

    resp_status_code = 404

    try:
        resp = requests.get(
            server + vmapi_query, params={"query": "up"}, timeout=timeout
        )
        resp_status_code = resp.status_code
        if resp.status_code != 200:
            logger.warning(
                f"⚠️ Could not connect to VictoriaMetrics server. Status code: {resp.status_code}"
            )
            return False
    except requests.exceptions.ProxyError as e:
        logger.warning(
            "⚠️ Proxy error connecting to VictoriaMetrics. "
            "Check HTTP(S)_PROXY and ensure NO_PROXY includes your VM host/domain (.ts.net). "
            f"Details: {e}"
        )
        return False
    except requests.exceptions.ConnectTimeout as e:
        logger.warning(
            "⚠️ Connect timeout to VictoriaMetrics. "
            "This usually means the host is unreachable (VPN/Tailscale down, firewall, or route issue). "
            f"Details: {e}"
        )
        return False
    except requests.exceptions.ConnectionError as e:
        logger.warning(
            "⚠️ Connection error to VictoriaMetrics. "
            "Check DNS, VPN/Tailscale connectivity, and the target port. "
            f"Details: {e}"
        )
        return False
    except Exception as e:
        logger.warning(f"⚠️ Error connecting to VictoriaMetrics server: {e}")
        return False

    return resp_status_code == 200


def delete_series_from_vm(
    server: str, match: str, timeout: int = 10
) -> requests.Response | None:
    logger = logging.getLogger("vm_delete_series")
    setup_simple_logger(logger, level=logging.INFO, format=LOG_FORMAT)

    try:
        resp = requests.post(
            server + vmapi_delete_series,
            params={"match[]": match},
            timeout=timeout,
        )

        if resp.status_code == 204:
            logging.info(f"✅ Successfully deleted series matching {match}")

        return resp
    except Exception as e:
        logging.error(f"⚠️ Error: {e}")
        return None


def get_metrics_from_vm(
    server: str,
    match: str,
    start_date: datetime,
    end_date: datetime,
    timeout: float = 10.0,
) -> dict:
    if "job" not in match:
        raise ValueError("Job name must be provided.")

    ret: dict = {}

    logging.basicConfig(level=LOG_LEVEL, format=LOG_FORMAT)

    def query_vm(single_hour: int):
        day_start = start_date + timedelta(hours=single_hour)
        day_end = min(day_start + timedelta(hours=1), end_date)

        logging.info(
            f"🔍 Querying from VictoriaMetrics for {match} from {day_start.isoformat()} to {day_end.isoformat()}..."
        )

        try:
            resp = requests.get(
                server + vmapi_query_range,
                params={
                    "query": f"{{{match}}}",
                    "start": day_start.timestamp(),
                    "end": day_end.timestamp(),
                    "step": "1s",
                },
                timeout=timeout,
            )

            if resp.status_code == 400:
                logging.warning(
                    f"⚠️ Could not connect to VictoriaMetrics server. Status code: {resp.status_code}"
                )
                return day_start.isoformat(), None

            elif resp.status_code == 422:
                logging.warning(
                    f"⚠️ Query error from VictoriaMetrics server. Status code: {resp.status_code}. Message: {resp.text}"
                )
                return day_start.isoformat(), None

            return day_start.isoformat(), resp.json()
        except Exception as e:
            logging.error(f"⚠️ Error connecting to VictoriaMetrics server: {e}")
            return day_start.isoformat(), None

    total_hours = (end_date - start_date).days * 24 + 1
    ret_lock = Lock()
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(query_vm, h) for h in range(total_hours)]
        for future in as_completed(futures):
            key, value = future.result()
            if value is not None:
                with ret_lock:
                    ret[key] = value
    return ret


def _escape_vm_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_vm_selector(
    job: str,
    metric_name: str = "",
    label_filters: dict[str, str] | None = None,
) -> str:
    filters = {"job": job, **(label_filters or {})}
    selector = ",".join(
        f'{key}="{_escape_vm_label_value(str(value))}"'
        for key, value in filters.items()
        if str(value).strip()
    )
    if metric_name.strip():
        return f'{metric_name}{{{selector}}}' if selector else metric_name
    return f"{{{selector}}}"


def get_latest_vm_job_timestamp(
    server: str,
    job: str,
    metric_name: str = "",
    label_filters: dict[str, str] | None = None,
    lookback: str = "365d",
    timeout: float = 10.0,
) -> dict[str, Any]:
    """
    Fast latest-sample lookup for one VM job.

    Uses one instant query with a wide lookback window so VictoriaMetrics can
    search for the most recent raw sample without a range-scan loop.
    """
    selector = _build_vm_selector(
        job=job, metric_name=metric_name, label_filters=label_filters
    )
    query = f"max(timestamp({selector}))"

    try:
        resp = requests.get(
            server + vmapi_query,
            params={"query": query, "step": lookback},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return {
                "has_data": False,
                "timestamp": None,
                "query": query,
                "error": f"status_code={resp.status_code}",
            }

        payload = resp.json()
        result = payload.get("data", {}).get("result", [])
        if not result:
            return {"has_data": False, "timestamp": None, "query": query}

        raw_value = result[0].get("value", [None, None])[1]
        if raw_value in (None, ""):
            return {"has_data": False, "timestamp": None, "query": query}

        latest_ts = datetime.fromtimestamp(
            float(raw_value), tz=timezone.utc
        )
        return {
            "has_data": True,
            "timestamp": latest_ts,
            "query": query,
        }
    except Exception as e:
        return {
            "has_data": False,
            "timestamp": None,
            "query": query,
            "error": str(e),
        }


def convert_mf4_to_trc(
    paths: list[Path | str], output_name: str | Path
) -> None:
    logger = logging.getLogger("mf4_to_trc_converter")
    setup_simple_logger(logger, level=logging.INFO, format=LOG_FORMAT)

    if not isinstance(paths, list):
        paths = [paths]

    if len(paths) == 0:
        raise ValueError("At least one input path must be provided.")

    if output_name == "":
        raise ValueError("Output name must be provided.")

    try:
        start_ts = time.time()
        if len(paths) > 1:
            logger.info(f"🔄 Concatenating {len(paths)} MF4 files...")
        mdf = MDF().concatenate(paths)
        logger.info(f"✅ Concatenation done in {get_time_str(start_ts)}")
        saved_path = mdf.save("temp.mf4", overwrite=True)
        start_ts = time.time()
        logger.info(f"🔄 Converting to TRC...")
        log = LogReader(saved_path)
        trc_logger = Logger(filename=Path(output_name).with_suffix(".trc"))
        for msg in log:
            trc_logger(msg)

        logger.info(
            f"✅ Saved at {Path(output_name).with_suffix('.trc')} (took {get_time_str(start_ts)})"
        )

        # Delete the temp file
        os.remove(saved_path)
    except KeyboardInterrupt:
        logger.warning("❌ Conversion cancelled by user.")
        return
    except Exception as e:
        logger.warning(f"❌ Error during conversion: {e}")


def get_windows_home_path() -> Path:
    """
    Try to get the windows home path on both Windows and WSL/Linux.
    Looks for a suitable path in the PATH environment variable or defaults to a common location.
    """
    if os.name == "nt":
        return Path(os.environ.get("USERPROFILE", str(Path.home())))

    # Linux/WSL path: if powershell is available, infer Windows profile path.
    if shutil.which("powershell.exe"):
        try:
            username = subprocess.run(
                ["powershell.exe", "Write-Host $env:USERNAME"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if username:
                candidate = Path(f"/mnt/c/Users/{username}")
                if candidate.exists():
                    return candidate
        except Exception:
            pass

    # Container-safe fallback.
    return Path.home()


def convert_to_eng(value: int | float | str) -> str:
    if not (type(value) is int or type(value) is float):
        try:
            logging.warning(
                f"⚠️ Value: `{value}` is of type {type(value)}, returning as string."
            )
            return str(value)
        except Exception:
            logging.error(f"❌ Cannot convert value to string: {value}")
            return ""

    if value > 1e9:
        return f"{value / 1e9:.3f}G"
    elif value > 1e6:
        return f"{value / 1e6:.3f}M"
    elif value > 1e3:
        return f"{value / 1e3:.3f}k"
    elif value is int:
        return f"{value}"
    else:
        return f"{value:.3f}"


if __name__ == "__main__":
    # Example usage
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    print(convert_to_eng(1234567890))  # Output: 1.235G
    print(convert_to_eng(1234567))  # Output: 1.235M
    print(convert_to_eng(1234))  # Output: 1.234k
    print(convert_to_eng(123))  # Output: 123
    print(convert_to_eng(123.456789))  # Output: 123.457
    print(convert_to_eng("test"))  # Output: test
