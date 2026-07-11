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

RMC_STATUS = {"A": "Valid", "V": "Warning"}
FIX_TYPE = {1: "No Fix", 2: "2D", 3: "3D"}

running = True
latest_gga = ""
latest_gga_lock = threading.Lock()
serial_write_lock = threading.Lock()


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
                with serial_write_lock:
                    ser.write(data)
                if now - last_rtcm_log >= 10:
                    print(f"[NTRIP] RTCM forwarded: {len(data)} bytes", flush=True)
                    last_rtcm_log = now
        except Exception as exc:  # noqa: BLE001
            if running:
                print(f"[NTRIP] Error: {exc}; reconnecting in 5s", flush=True)
                time.sleep(5)


def post_payload(endpoint: str, payload: dict[str, Any], timeout: float) -> int:
    response = requests.post(
        endpoint,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        timeout=timeout,
    )
    return response.status_code


def spooled_payloads(spool_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(spool_dir.glob("*.json"))


def prune_spool(spool_dir: pathlib.Path, max_files: int) -> None:
    if max_files <= 0:
        return
    files = spooled_payloads(spool_dir)
    overflow = len(files) - max_files
    if overflow <= 0:
        return
    for path in files[:overflow]:
        try:
            path.unlink()
            print(f"[UNIFIED] Spool full; dropped oldest payload {path.name}", flush=True)
        except OSError as exc:
            print(f"[UNIFIED] Failed to prune {path.name}: {exc}", flush=True)


def spool_payload(spool_dir: pathlib.Path, payload: dict[str, Any], max_files: int) -> pathlib.Path:
    spool_dir.mkdir(parents=True, exist_ok=True)
    sent_at = str(payload.get("sent_at") or dt.datetime.now(dt.UTC).isoformat())
    safe_sent_at = "".join(ch if ch.isalnum() else "-" for ch in sent_at)
    filename = f"{safe_sent_at}-{time.time_ns()}.json"
    tmp_path = spool_dir / f".{filename}.tmp"
    final_path = spool_dir / filename
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp_path, final_path)
    prune_spool(spool_dir, max_files)
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
    spool_dir.mkdir(parents=True, exist_ok=True)

    while running:
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
                wake_event.wait(args.post_retry_delay)
                wake_event.clear()
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
    parser.add_argument("--spool-dir", default=os.environ.get("UNIFIED_SPOOL_DIR", "unified_spool"))
    parser.add_argument(
        "--max-spool-files",
        type=int,
        default=int(os.environ.get("UNIFIED_MAX_SPOOL_FILES", os.environ.get("UNIFIED_MAX_PENDING", "7200"))),
    )
    parser.add_argument("--flush-burst", type=int, default=int(os.environ.get("UNIFIED_FLUSH_BURST", "3")))
    parser.add_argument("--post-retry-delay", type=float, default=float(os.environ.get("UNIFIED_POST_RETRY_DELAY", "5")))
    parser.add_argument("--ntrip-host", default=os.environ.get("NTRIP_HOST", "qrtksa1.quectel.com"))
    parser.add_argument("--ntrip-port", type=int, default=int(os.environ.get("NTRIP_PORT", "2101")))
    parser.add_argument("--ntrip-mount", default=os.environ.get("NTRIP_MOUNT", "AUTO"))
    parser.add_argument("--ntrip-user", default=os.environ.get("NTRIP_USER", ""))
    parser.add_argument("--ntrip-pass", default=os.environ.get("NTRIP_PASS", ""))
    parser.add_argument("--no-ntrip", action="store_true")
    parser.add_argument("--max-posts", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    global latest_gga
    global running

    args = parse_args()
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"[MAIN] Opening {args.serial_port} @ {args.baud}", flush=True)
    ser = serial.Serial(args.serial_port, args.baud, timeout=1)
    time.sleep(0.2)
    with serial_write_lock:
        ser.write(b"$PQTMCFGRTK,W,1,1*6C\r\n")

    if not args.no_ntrip:
        threading.Thread(target=ntrip_worker, args=(args, ser), daemon=True).start()

    snapshot = Snapshot()
    spool_dir = pathlib.Path(args.spool_dir)
    send_wake = threading.Event()
    threading.Thread(target=unified_worker, args=(args, send_wake), daemon=True).start()
    next_post = time.monotonic() + args.interval
    try:
        while running:
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
                spool_payload(spool_dir, payload, args.max_spool_files)
                snapshot.reset()
                send_wake.set()
    finally:
        running = False
        send_wake.set()
        try:
            ser.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[MAIN] Serial close error ignored: {exc}", flush=True)
        print("[MAIN] Stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
