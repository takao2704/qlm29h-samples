#!/usr/bin/env python3
"""Forward NTRIP corrections to QLM29H and send parsed NMEA JSON to SORACOM Unified Endpoint."""

from __future__ import annotations

import argparse
import base64
import collections
import datetime as dt
import json
import os
import pathlib
import signal
import socket
import sys
import threading
import time
from typing import Any

import requests
import serial


GGA_QUALITY = {
    0: "No Fix",
    1: "GPS SPS Mode",
    2: "Differential GPS / SPS / SBAS Mode",
    4: "Fixed RTK",
    5: "Float RTK",
    6: "Estimated / Dead Reckoning",
}

DEFAULT_DISK_SPOOL_DIR = "unified_spool"
DEFAULT_RAM_SPOOL_DIR = "/dev/shm/qlm29h-nmea-unified/spool"

RMC_STATUS = {"A": "Valid", "V": "Warning"}
FIX_TYPE = {1: "No Fix", 2: "2D", 3: "3D"}

running = True
latest_gga = ""
latest_gga_lock = threading.Lock()
serial_write_lock = threading.Lock()
fatal_serial_error = threading.Event()
fatal_serial_error_message = ""
fatal_serial_error_lock = threading.Lock()


def to_int(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def to_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def compact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: compact(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [compact(v) for v in value if v not in (None, "", [], {})]
    return value


def nmea_checksum(data: str) -> str:
    checksum = 0
    for char in data:
        checksum ^= ord(char)
    return f"{checksum:02X}"


def build_receiver_command(command: str) -> bytes:
    return f"${command}*{nmea_checksum(command)}\r\n".encode("ascii")


def receiver_startup_commands(dr_state: str) -> list[tuple[str, bytes]]:
    if dr_state not in ("on", "off", "unchanged"):
        raise ValueError("dr_state must be on, off, or unchanged")
    commands = [("RTK on", build_receiver_command("PQTMCFGRTK,W,1,1"))]
    if dr_state != "unchanged":
        state = 1 if dr_state == "on" else 0
        commands.append((f"DR {dr_state}", build_receiver_command(f"PQTMCFGDR,W,{state}")))
    return commands


def parse_coord(raw: str | None, hemisphere: str | None) -> float | None:
    if not raw or not hemisphere:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    degrees = int(value // 100)
    minutes = value - degrees * 100
    decimal = degrees + minutes / 60.0
    if hemisphere in ("S", "W"):
        decimal = -decimal
    return decimal


def format_utc_time(raw: str | None) -> str | None:
    if not raw or len(raw) < 6:
        return raw
    hh, mm, ss = raw[0:2], raw[2:4], raw[4:]
    return f"{hh}:{mm}:{ss}Z"


def format_utc_date(raw: str | None) -> str | None:
    if not raw or len(raw) != 6:
        return raw
    day, month, yy = int(raw[0:2]), int(raw[2:4]), int(raw[4:6])
    year = 1900 + yy if yy >= 80 else 2000 + yy
    return f"{year:04d}-{month:02d}-{day:02d}"


def named_fields(names: list[str], values: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for idx, value in enumerate(values):
        key = names[idx] if idx < len(names) else f"field_{idx + 1:02d}"
        out[key] = value
    return out


def parse_gga(values: list[str]) -> dict[str, Any]:
    names = [
        "utc_time_raw",
        "latitude_raw",
        "latitude_hemisphere",
        "longitude_raw",
        "longitude_hemisphere",
        "fix_quality",
        "satellites_used",
        "hdop",
        "altitude",
        "altitude_unit",
        "geoid_separation",
        "geoid_separation_unit",
        "differential_age",
        "differential_station_id",
    ]
    fields = named_fields(names, values)
    quality = to_int(fields.get("fix_quality"))
    fields.update(
        {
            "utc_time": format_utc_time(fields.get("utc_time_raw")),
            "latitude": parse_coord(fields.get("latitude_raw"), fields.get("latitude_hemisphere")),
            "longitude": parse_coord(fields.get("longitude_raw"), fields.get("longitude_hemisphere")),
            "fix_quality": quality,
            "fix_quality_label": GGA_QUALITY.get(quality, f"Unknown({quality})") if quality is not None else None,
            "satellites_used": to_int(fields.get("satellites_used")),
            "hdop": to_float(fields.get("hdop")),
            "altitude": to_float(fields.get("altitude")),
            "geoid_separation": to_float(fields.get("geoid_separation")),
            "differential_age": to_float(fields.get("differential_age")),
        }
    )
    return fields


def parse_rmc(values: list[str]) -> dict[str, Any]:
    names = [
        "utc_time_raw",
        "status",
        "latitude_raw",
        "latitude_hemisphere",
        "longitude_raw",
        "longitude_hemisphere",
        "speed_knots",
        "course_degrees",
        "date_raw",
        "magnetic_variation",
        "magnetic_variation_direction",
        "mode",
        "nav_status",
    ]
    fields = named_fields(names, values)
    speed_knots = to_float(fields.get("speed_knots"))
    fields.update(
        {
            "utc_time": format_utc_time(fields.get("utc_time_raw")),
            "date": format_utc_date(fields.get("date_raw")),
            "status_label": RMC_STATUS.get(fields.get("status")),
            "latitude": parse_coord(fields.get("latitude_raw"), fields.get("latitude_hemisphere")),
            "longitude": parse_coord(fields.get("longitude_raw"), fields.get("longitude_hemisphere")),
            "speed_knots": speed_knots,
            "speed_kmh": speed_knots * 1.852 if speed_knots is not None else None,
            "course_degrees": to_float(fields.get("course_degrees")),
            "magnetic_variation": to_float(fields.get("magnetic_variation")),
        }
    )
    return fields


def parse_gll(values: list[str]) -> dict[str, Any]:
    names = [
        "latitude_raw",
        "latitude_hemisphere",
        "longitude_raw",
        "longitude_hemisphere",
        "utc_time_raw",
        "status",
        "mode",
    ]
    fields = named_fields(names, values)
    fields.update(
        {
            "utc_time": format_utc_time(fields.get("utc_time_raw")),
            "status_label": RMC_STATUS.get(fields.get("status")),
            "latitude": parse_coord(fields.get("latitude_raw"), fields.get("latitude_hemisphere")),
            "longitude": parse_coord(fields.get("longitude_raw"), fields.get("longitude_hemisphere")),
        }
    )
    return fields


def parse_vtg(values: list[str]) -> dict[str, Any]:
    names = [
        "course_true_degrees",
        "true_indicator",
        "course_magnetic_degrees",
        "magnetic_indicator",
        "speed_knots",
        "knots_unit",
        "speed_kmh",
        "kmh_unit",
        "mode",
    ]
    fields = named_fields(names, values)
    fields.update(
        {
            "course_true_degrees": to_float(fields.get("course_true_degrees")),
            "course_magnetic_degrees": to_float(fields.get("course_magnetic_degrees")),
            "speed_knots": to_float(fields.get("speed_knots")),
            "speed_kmh": to_float(fields.get("speed_kmh")),
        }
    )
    return fields


def parse_gsa(values: list[str]) -> dict[str, Any]:
    fix_type = to_int(values[1] if len(values) > 1 else None)
    fields = {
        "mode": values[0] if len(values) > 0 else None,
        "fix_type": fix_type,
        "fix_type_label": FIX_TYPE.get(fix_type, f"Unknown({fix_type})") if fix_type is not None else None,
        "satellite_ids": [value for value in values[2:14] if value],
        "pdop": to_float(values[14] if len(values) > 14 else None),
        "hdop": to_float(values[15] if len(values) > 15 else None),
        "vdop": to_float(values[16] if len(values) > 16 else None),
        "system_id": values[17] if len(values) > 17 else None,
    }
    for idx, value in enumerate(values):
        fields[f"raw_field_{idx + 1:02d}"] = value
    return fields


def parse_gsv(values: list[str]) -> dict[str, Any]:
    total_messages = to_int(values[0] if len(values) > 0 else None)
    message_number = to_int(values[1] if len(values) > 1 else None)
    satellites_in_view = to_int(values[2] if len(values) > 2 else None)
    rest = values[3:]
    signal_id = None
    if len(rest) % 4 == 1:
        signal_id = rest[-1]
        rest = rest[:-1]

    satellites = []
    for index in range(0, len(rest), 4):
        chunk = rest[index : index + 4]
        if len(chunk) < 4:
            continue
        satellites.append(
            compact(
                {
                    "satellite_id": chunk[0],
                    "elevation_degrees": to_int(chunk[1]),
                    "azimuth_degrees": to_int(chunk[2]),
                    "snr_dbhz": to_int(chunk[3]),
                }
            )
        )

    fields = {
        "total_messages": total_messages,
        "message_number": message_number,
        "satellites_in_view": satellites_in_view,
        "satellites": satellites,
        "signal_id": signal_id,
    }
    for idx, value in enumerate(values):
        fields[f"raw_field_{idx + 1:02d}"] = value
    return fields


def generic_fields(values: list[str]) -> dict[str, Any]:
    return {f"field_{idx + 1:02d}": value for idx, value in enumerate(values)}


def parse_nmea(sentence: str) -> dict[str, Any] | None:
    line = sentence.strip()
    if not line.startswith("$"):
        return None

    checksum_text = None
    checksum_valid = None
    data = line[1:]
    if "*" in data:
        data, checksum_text = data.split("*", 1)
        checksum_text = checksum_text[:2].upper()
        checksum_valid = nmea_checksum(data) == checksum_text

    parts = data.split(",")
    sentence_id = parts[0]
    if len(sentence_id) < 3:
        return None

    talker = sentence_id[:2]
    message_type = sentence_id[2:]
    values = parts[1:]
    parser = {
        "GGA": parse_gga,
        "RMC": parse_rmc,
        "GLL": parse_gll,
        "VTG": parse_vtg,
        "GSA": parse_gsa,
        "GSV": parse_gsv,
    }.get(message_type, generic_fields)

    return compact(
        {
            "raw": line,
            "sentence_id": sentence_id,
            "talker": talker,
            "message_type": message_type,
            "checksum": checksum_text,
            "checksum_valid": checksum_valid,
            "raw_fields": values,
            "fields": parser(values),
            "received_at": dt.datetime.now(dt.UTC).isoformat(),
        }
    )


class Snapshot:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.started_at = dt.datetime.now(dt.UTC)
        self.latest: dict[str, dict[str, Any]] = {}
        self.multi: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
        self.counts: collections.Counter[str] = collections.Counter()

    def update(self, parsed: dict[str, Any]) -> None:
        sentence_id = parsed["sentence_id"]
        message_type = parsed["message_type"]
        self.counts[sentence_id] += 1

        if message_type == "GSA":
            fields = parsed.get("fields", {})
            key = fields.get("system_id") or parsed.get("talker") or str(len(self.multi[sentence_id]))
            self.multi[sentence_id][str(key)] = parsed
        elif message_type == "GSV":
            fields = parsed.get("fields", {})
            key = f"{fields.get('signal_id', '')}:{fields.get('message_number', '')}"
            self.multi[sentence_id][key] = parsed
        else:
            self.latest[sentence_id] = parsed

    def build_payload(self, serial_port: str) -> dict[str, Any]:
        ended_at = dt.datetime.now(dt.UTC)
        sentences: dict[str, Any] = dict(self.latest)
        for sentence_id, entries in self.multi.items():
            sentences[sentence_id] = list(entries.values())

        latest_position = self._latest_position(sentences)
        payload: dict[str, Any] = {
            "source": "qlm29h_nmea",
            "serial_port": serial_port,
            "sent_at": ended_at.isoformat(),
            "window": {
                "started_at": self.started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_sec": round((ended_at - self.started_at).total_seconds(), 3),
            },
            "sentence_counts": dict(self.counts),
            "nmea": sentences,
        }
        if latest_position:
            payload["latest_position"] = latest_position
            payload.update({k: v for k, v in latest_position.items() if k in ("lat", "lon", "quality", "quality_label")})
        return compact(payload)

    def _latest_position(self, sentences: dict[str, Any]) -> dict[str, Any]:
        for sentence_id in ("GNGGA", "GPGGA", "GNRMC", "GPRMC", "GNGLL", "GPGLL"):
            item = sentences.get(sentence_id)
            if not isinstance(item, dict):
                continue
            fields = item.get("fields", {})
            lat = fields.get("latitude")
            lon = fields.get("longitude")
            if lat is None or lon is None:
                continue
            out = {
                "lat": lat,
                "lon": lon,
                "sentence_id": sentence_id,
                "utc_time": fields.get("utc_time"),
                "quality": fields.get("fix_quality"),
                "quality_label": fields.get("fix_quality_label"),
                "satellites_used": fields.get("satellites_used"),
                "hdop": fields.get("hdop"),
                "altitude": fields.get("altitude"),
            }
            return compact(out)
        return {}

    def has_data(self) -> bool:
        return bool(self.latest or self.multi)


def build_ntrip_request(host: str, mount: str, username: str, password: str) -> bytes:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = (
        f"GET /{mount} HTTP/1.0\r\n"
        "User-Agent: NTRIP qlm29h-nmea-unified/1.0\r\n"
        f"Host: {host}\r\n"
        f"Authorization: Basic {token}\r\n"
        "Ntrip-Version: Ntrip/1.0\r\n"
        "\r\n"
    )
    return request.encode()


def signal_fatal_serial_error(message: str) -> None:
    global fatal_serial_error_message

    with fatal_serial_error_lock:
        fatal_serial_error_message = message
    fatal_serial_error.set()


def get_fatal_serial_error_message() -> str:
    with fatal_serial_error_lock:
        return fatal_serial_error_message


def wait_while_running(seconds: float) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while running:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.5, remaining))


def ntrip_worker(args: argparse.Namespace, ser: serial.Serial) -> None:
    if not args.ntrip_user or not args.ntrip_pass:
        print("[NTRIP] Disabled: NTRIP_USER/NTRIP_PASS are not set", flush=True)
        return

    while running:
        try:
            print(f"[NTRIP] Connecting to {args.ntrip_host}:{args.ntrip_port}", flush=True)
            sock = socket.create_connection((args.ntrip_host, args.ntrip_port), timeout=10)
            sock.sendall(build_ntrip_request(args.ntrip_host, args.ntrip_mount, args.ntrip_user, args.ntrip_pass))
            response = sock.recv(4096)
            if b"200" not in response.split(b"\r\n", 1)[0]:
                print(f"[NTRIP] Connection rejected: {response[:120]!r}", flush=True)
                sock.close()
                time.sleep(10)
                continue
            print("[NTRIP] Connected", flush=True)
            sock.settimeout(1)
            last_gga_sent = 0.0
            last_rtcm_log = 0.0
            while running:
                now = time.monotonic()
                with latest_gga_lock:
                    gga = latest_gga
                if gga and now - last_gga_sent >= 1.0:
                    sock.sendall((gga.strip() + "\r\n").encode())
                    last_gga_sent = now
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    raise ConnectionError("NTRIP socket closed")
                try:
                    with serial_write_lock:
                        ser.write(data)
                except (OSError, serial.SerialException) as exc:
                    signal_fatal_serial_error(f"NTRIP serial write failed: {exc}")
                    return
                if now - last_rtcm_log >= 10:
                    print(f"[NTRIP] RTCM forwarded: {len(data)} bytes", flush=True)
                    last_rtcm_log = now
        except Exception as exc:  # noqa: BLE001
            if running:
                print(f"[NTRIP] Error: {exc}; reconnecting in 5s", flush=True)
                wait_while_running(5)


def post_payload(endpoint: str, payload: dict[str, Any], timeout: float) -> int:
    response = requests.post(
        endpoint,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        timeout=timeout,
    )
    return response.status_code


def resolve_spool_dir(args: argparse.Namespace) -> pathlib.Path:
    if args.spool_dir:
        return pathlib.Path(args.spool_dir)
    if args.spool_storage == "ram":
        return pathlib.Path(DEFAULT_RAM_SPOOL_DIR)
    return pathlib.Path(DEFAULT_DISK_SPOOL_DIR)


def spooled_payloads(spool_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(spool_dir.glob("*.json"))


def spool_file_sizes(files: list[pathlib.Path]) -> list[tuple[pathlib.Path, int]]:
    sized_files = []
    for path in files:
        try:
            sized_files.append((path, path.stat().st_size))
        except FileNotFoundError:
            continue
    return sized_files


def drop_oldest_payload(spool_dir: pathlib.Path) -> bool:
    files = spooled_payloads(spool_dir)
    if not files:
        return False
    path = files[0]
    try:
        path.unlink()
        print(f"[UNIFIED] Spool full; dropped oldest payload {path.name}", flush=True)
        return True
    except OSError as exc:
        print(f"[UNIFIED] Failed to prune {path.name}: {exc}", flush=True)
        return False


def prune_spool(
    spool_dir: pathlib.Path,
    max_files: int,
    max_bytes: int,
    reserve_files: int = 0,
    reserve_bytes: int = 0,
) -> None:
    if max_files <= 0 and max_bytes <= 0:
        return

    if max_bytes > 0:
        sized_files = spool_file_sizes(spooled_payloads(spool_dir))
    else:
        sized_files = [(path, 0) for path in spooled_payloads(spool_dir)]
    total_bytes = sum(size for _, size in sized_files)

    def over_limit() -> bool:
        file_limit_hit = max_files > 0 and len(sized_files) + reserve_files > max_files
        byte_limit_hit = max_bytes > 0 and total_bytes + reserve_bytes > max_bytes
        return file_limit_hit or byte_limit_hit

    while sized_files and over_limit():
        path, size = sized_files.pop(0)
        try:
            path.unlink()
            total_bytes -= size
            print(f"[UNIFIED] Spool full; dropped oldest payload {path.name}", flush=True)
        except OSError as exc:
            print(f"[UNIFIED] Failed to prune {path.name}: {exc}", flush=True)


def cleanup_tmp_payload(tmp_path: pathlib.Path) -> None:
    try:
        tmp_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"[UNIFIED] Failed to remove temp payload {tmp_path.name}: {exc}", flush=True)


def write_payload_file(tmp_path: pathlib.Path, final_path: pathlib.Path, payload_bytes: bytes) -> None:
    tmp_path.write_bytes(payload_bytes)
    os.replace(tmp_path, final_path)


def spool_payload(
    spool_dir: pathlib.Path,
    payload: dict[str, Any],
    max_files: int,
    max_bytes: int,
) -> pathlib.Path | None:
    payload_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if max_bytes > 0 and len(payload_bytes) > max_bytes:
        print(
            f"[UNIFIED] Dropped payload: size={len(payload_bytes)} exceeds max_spool_bytes={max_bytes}",
            flush=True,
        )
        return None

    try:
        spool_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[UNIFIED] Dropped payload: cannot create spool dir {spool_dir}: {exc}", flush=True)
        return None

    sent_at = str(payload.get("sent_at") or dt.datetime.now(dt.UTC).isoformat())
    safe_sent_at = "".join(ch if ch.isalnum() else "-" for ch in sent_at)
    filename = f"{safe_sent_at}-{time.time_ns()}.json"
    tmp_path = spool_dir / f".{filename}.tmp"
    final_path = spool_dir / filename
    prune_spool(spool_dir, max_files, max_bytes, reserve_files=1, reserve_bytes=len(payload_bytes))
    try:
        write_payload_file(tmp_path, final_path, payload_bytes)
    except OSError as exc:
        print(f"[UNIFIED] Spool write failed: {exc}; pruning one payload and retrying once", flush=True)
        cleanup_tmp_payload(tmp_path)
        if not drop_oldest_payload(spool_dir):
            return None
        try:
            write_payload_file(tmp_path, final_path, payload_bytes)
        except OSError as retry_exc:
            print(f"[UNIFIED] Dropped payload after retry: {retry_exc}", flush=True)
            cleanup_tmp_payload(tmp_path)
            return None

    prune_spool(spool_dir, max_files, max_bytes)
    return final_path


def load_spooled_payload(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("spooled payload is not a JSON object")
    return payload


def mark_bad_payload(path: pathlib.Path) -> None:
    bad_path = path.with_suffix(path.suffix + ".bad")
    try:
        os.replace(path, bad_path)
    except OSError:
        pass


def unified_worker(args: argparse.Namespace, wake_event: threading.Event) -> None:
    global running

    posts = 0
    spool_dir = pathlib.Path(args.spool_dir)

    while running:
        try:
            spool_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(f"[UNIFIED] Cannot create spool dir {spool_dir}: {exc}; retrying", flush=True)
            wait_while_running(args.post_retry_delay)
            continue

        files = spooled_payloads(spool_dir)
        if not files:
            wake_event.wait(1.0)
            wake_event.clear()
            continue

        sent_now = 0
        for path in files[: max(1, args.flush_burst)]:
            try:
                queued = load_spooled_payload(path)
            except Exception as exc:  # noqa: BLE001
                print(f"[UNIFIED] Bad spooled payload {path.name}: {exc}; quarantining", flush=True)
                mark_bad_payload(path)
                continue

            try:
                status = post_payload(args.endpoint, queued, args.post_timeout)
                if status < 200 or status >= 300:
                    raise RuntimeError(f"HTTP {status}")
            except Exception as exc:  # noqa: BLE001
                print(f"[UNIFIED] Error: {exc}; pending={len(spooled_payloads(spool_dir))}", flush=True)
                wait_while_running(args.post_retry_delay)
                break

            try:
                path.unlink()
            except FileNotFoundError:
                pass

            posts += 1
            sent_now += 1
            sentence_count = sum(queued.get("sentence_counts", {}).values())
            quality = queued.get("quality_label", "-")
            queue_left = len(spooled_payloads(spool_dir))
            print(
                f"[UNIFIED] Sent post={posts} sentences={sentence_count} "
                f"quality={quality} pending={queue_left} HTTP {status}",
                flush=True,
            )

            if args.max_posts and posts >= args.max_posts:
                running = False
                wake_event.set()
                return

        if sent_now == 0:
            wake_event.wait(0.2)
            wake_event.clear()


def handle_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
    global running
    running = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serial-port", default=os.environ.get("SERIAL_PORT", "/dev/ttyUSB0"))
    parser.add_argument("--baud", type=int, default=int(os.environ.get("SERIAL_BAUD", "115200")))
    parser.add_argument("--endpoint", default=os.environ.get("UNIFIED_ENDPOINT", "http://unified.soracom.io"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("UNIFIED_INTERVAL", "5")))
    parser.add_argument("--post-timeout", type=float, default=10)
    parser.add_argument("--max-pending", type=int, default=int(os.environ.get("UNIFIED_MAX_PENDING", "120")))
    parser.add_argument(
        "--spool-storage",
        choices=("disk", "ram"),
        default=os.environ.get("UNIFIED_SPOOL_STORAGE", "disk").lower(),
        help="Spool storage class used when --spool-dir is not set.",
    )
    parser.add_argument(
        "--spool-dir",
        default=os.environ.get("UNIFIED_SPOOL_DIR"),
        help="Explicit spool directory. Overrides --spool-storage default paths.",
    )
    parser.add_argument(
        "--max-spool-files",
        type=int,
        default=int(os.environ.get("UNIFIED_MAX_SPOOL_FILES", os.environ.get("UNIFIED_MAX_PENDING", "7200"))),
    )
    parser.add_argument(
        "--max-spool-bytes",
        type=int,
        default=int(os.environ.get("UNIFIED_MAX_SPOOL_BYTES", "0")),
        help="Maximum total bytes for spooled JSON payloads. 0 disables the byte limit.",
    )
    parser.add_argument("--flush-burst", type=int, default=int(os.environ.get("UNIFIED_FLUSH_BURST", "3")))
    parser.add_argument("--post-retry-delay", type=float, default=float(os.environ.get("UNIFIED_POST_RETRY_DELAY", "5")))
    parser.add_argument("--ntrip-host", default=os.environ.get("NTRIP_HOST", "qrtksa1.quectel.com"))
    parser.add_argument("--ntrip-port", type=int, default=int(os.environ.get("NTRIP_PORT", "2101")))
    parser.add_argument("--ntrip-mount", default=os.environ.get("NTRIP_MOUNT", "AUTO"))
    parser.add_argument("--ntrip-user", default=os.environ.get("NTRIP_USER", ""))
    parser.add_argument("--ntrip-pass", default=os.environ.get("NTRIP_PASS", ""))
    parser.add_argument(
        "--dr-state",
        choices=("on", "off", "unchanged"),
        default=os.environ.get("QLM29H_DR_STATE", "on").strip().lower(),
        help="Set QLM29H DR state at startup, or leave the receiver setting unchanged.",
    )
    parser.add_argument("--no-ntrip", action="store_true")
    parser.add_argument("--max-posts", type=int, default=0)
    args = parser.parse_args()
    args.spool_dir = str(resolve_spool_dir(args))
    return args


def main() -> int:
    global latest_gga
    global running

    args = parse_args()
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"[MAIN] Opening {args.serial_port} @ {args.baud}", flush=True)
    ser = None
    try:
        ser = serial.Serial(args.serial_port, args.baud, timeout=1)
        time.sleep(0.2)
        for label, command in receiver_startup_commands(args.dr_state):
            with serial_write_lock:
                ser.write(command)
            print(f"[MAIN] Receiver command ({label}): {command.decode().strip()}", flush=True)
            time.sleep(0.2)
    except (OSError, serial.SerialException) as exc:
        print(f"[MAIN] Serial setup error: {exc}; exiting for systemd restart", flush=True)
        if ser is not None:
            try:
                ser.close()
            except Exception as close_exc:  # noqa: BLE001
                print(f"[MAIN] Serial close error ignored: {close_exc}", flush=True)
        return 1

    if not args.no_ntrip:
        threading.Thread(target=ntrip_worker, args=(args, ser), daemon=True).start()

    snapshot = Snapshot()
    spool_dir = pathlib.Path(args.spool_dir)
    print(
        f"[UNIFIED] Spool storage={args.spool_storage} dir={spool_dir} "
        f"max_files={args.max_spool_files} max_bytes={args.max_spool_bytes}",
        flush=True,
    )
    send_wake = threading.Event()
    threading.Thread(target=unified_worker, args=(args, send_wake), daemon=True).start()
    next_post = time.monotonic() + args.interval
    try:
        while running:
            if fatal_serial_error.is_set():
                print(f"[MAIN] Fatal serial error: {get_fatal_serial_error_message()}", flush=True)
                return 1
            try:
                raw = ser.readline().decode(errors="ignore").strip()
            except (OSError, serial.SerialException) as exc:
                print(f"[MAIN] Serial error: {exc}; exiting for systemd restart", flush=True)
                return 1
            if raw:
                parsed = parse_nmea(raw)
                if parsed:
                    snapshot.update(parsed)
                    if parsed["message_type"] == "GGA":
                        with latest_gga_lock:
                            latest_gga = parsed["raw"]

            now = time.monotonic()
            if now >= next_post:
                next_post = now + args.interval
                if not snapshot.has_data():
                    continue
                payload = snapshot.build_payload(args.serial_port)
                spooled_path = spool_payload(spool_dir, payload, args.max_spool_files, args.max_spool_bytes)
                if spooled_path is None:
                    print("[UNIFIED] Current payload was not spooled; resetting snapshot", flush=True)
                snapshot.reset()
                send_wake.set()
    finally:
        running = False
        send_wake.set()
        if ser is not None:
            try:
                ser.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[MAIN] Serial close error ignored: {exc}", flush=True)
        print("[MAIN] Stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
