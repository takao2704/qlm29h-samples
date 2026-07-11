#!/usr/bin/env python3
"""Monitor and save QLM29H dead-reckoning driving calibration."""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable

import serial


CAL_STATE_LABELS = {
    0: "not calibrated",
    1: "lightly calibrated",
    2: "fully calibrated",
    3: "fully calibrated with high-precision heading",
}

NAV_TYPE_LABELS = {
    0: "no position",
    1: "GNSS only",
    2: "DR only",
    3: "GNSS + DR",
}


@dataclasses.dataclass(frozen=True)
class CalibrationStatus:
    message_version: int
    calibration_state: int
    navigation_type: int
    raw: str

    @property
    def calibration_label(self) -> str:
        return CAL_STATE_LABELS.get(self.calibration_state, "unknown")

    @property
    def navigation_label(self) -> str:
        return NAV_TYPE_LABELS.get(self.navigation_type, "unknown")


class ReceiverCommandError(RuntimeError):
    pass


def nmea_checksum(body: str) -> str:
    checksum = 0
    for char in body:
        checksum ^= ord(char)
    return f"{checksum:02X}"


def build_command(body: str) -> bytes:
    return f"${body}*{nmea_checksum(body)}\r\n".encode("ascii")


def checksum_valid(line: str) -> bool:
    if not line.startswith("$") or "*" not in line:
        return False
    body, supplied = line[1:].split("*", 1)
    return nmea_checksum(body) == supplied[:2].upper()


def parse_calibration_status(line: str) -> CalibrationStatus | None:
    if not line.startswith("$PQTMDRCAL,") or not checksum_valid(line):
        return None
    body = line[1:].split("*", 1)[0]
    fields = body.split(",")
    if len(fields) != 4:
        return None
    try:
        return CalibrationStatus(
            message_version=int(fields[1]),
            calibration_state=int(fields[2]),
            navigation_type=int(fields[3]),
            raw=line,
        )
    except ValueError:
        return None


def parse_message_rate(line: str) -> int | None:
    prefix = "$PQTMCFGMSGRATE,OK,PQTMDRCAL,"
    if not line.startswith(prefix) or not checksum_valid(line):
        return None
    value = line[len(prefix) :].split(",", 1)[0].split("*", 1)[0]
    try:
        return int(value)
    except ValueError:
        return None


def read_matching_line(receiver: serial.Serial, prefixes: Iterable[str], timeout: float) -> str | None:
    prefixes = tuple(prefixes)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = receiver.readline().decode("ascii", errors="ignore").strip()
        if line.startswith(prefixes):
            return line
    return None


def send_and_expect(
    receiver: serial.Serial,
    body: str,
    success_prefix: str,
    error_prefix: str,
    timeout: float = 3,
) -> str:
    receiver.write(build_command(body))
    receiver.flush()
    response = read_matching_line(receiver, (success_prefix, error_prefix), timeout)
    if response is None:
        raise ReceiverCommandError(f"No response to ${body}")
    if response.startswith(error_prefix):
        raise ReceiverCommandError(response)
    return response


def get_calibration_message_rate(receiver: serial.Serial) -> int:
    response = send_and_expect(
        receiver,
        "PQTMCFGMSGRATE,R,PQTMDRCAL,1",
        "$PQTMCFGMSGRATE,OK,PQTMDRCAL,",
        "$PQTMCFGMSGRATE,ERROR,",
    )
    rate = parse_message_rate(response)
    if rate is None:
        raise ReceiverCommandError(f"Unexpected message-rate response: {response}")
    return rate


def set_calibration_message_rate(receiver: serial.Serial, rate: int) -> None:
    send_and_expect(
        receiver,
        f"PQTMCFGMSGRATE,W,PQTMDRCAL,{rate},1",
        "$PQTMCFGMSGRATE,OK*",
        "$PQTMCFGMSGRATE,ERROR,",
    )


def enable_dr(receiver: serial.Serial) -> None:
    send_and_expect(
        receiver,
        "PQTMCFGDR,W,1",
        "$PQTMCFGDR,OK*",
        "$PQTMCFGDR,ERROR,",
    )


def set_dr_hot_start(receiver: serial.Serial, mode: int) -> None:
    send_and_expect(
        receiver,
        f"PQTMCFGDRHOT,W,{mode}",
        "$PQTMCFGDRHOT,OK*",
        "$PQTMCFGDRHOT,ERROR,",
    )


def clear_calibration(receiver: serial.Serial) -> None:
    send_and_expect(
        receiver,
        "PQTMDRCLR",
        "$PQTMDRCLR,OK*",
        "$PQTMDRCLR,ERROR,",
    )


def save_calibration(receiver: serial.Serial) -> None:
    send_and_expect(
        receiver,
        "PQTMDRSAVE",
        "$PQTMDRSAVE,OK*",
        "$PQTMDRSAVE,ERROR,",
    )


def read_calibration_status(receiver: serial.Serial, timeout: float) -> CalibrationStatus | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = read_matching_line(receiver, ("$PQTMDRCAL,",), min(1, deadline - time.monotonic()))
        if line is None:
            continue
        status = parse_calibration_status(line)
        if status is not None:
            return status
    return None


def service_is_active(service_name: str) -> bool:
    if not service_name or shutil.which("systemctl") is None:
        return False
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", service_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def print_status(status: CalibrationStatus, elapsed: float | None = None) -> None:
    prefix = "[STATUS]"
    if elapsed is not None:
        prefix = f"[STATUS {elapsed:6.1f}s]"
    print(
        f"{prefix} CalState={status.calibration_state} ({status.calibration_label}), "
        f"NavType={status.navigation_type} ({status.navigation_label})",
        flush=True,
    )


def monitor_calibration(
    receiver: serial.Serial,
    timeout: float,
    minimum_state: int,
) -> CalibrationStatus | None:
    started = time.monotonic()
    last_state: tuple[int, int] | None = None
    last_report = 0.0

    while time.monotonic() - started < timeout:
        status = read_calibration_status(receiver, min(5, timeout - (time.monotonic() - started)))
        if status is None:
            print("[WARN] No PQTMDRCAL message received in the last 5 seconds", flush=True)
            continue

        elapsed = time.monotonic() - started
        state = (status.calibration_state, status.navigation_type)
        if state != last_state or elapsed - last_report >= 15:
            print_status(status, elapsed)
            last_state = state
            last_report = elapsed

        if status.calibration_state >= minimum_state:
            return status

    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serial-port",
        default=os.environ.get("SERIAL_PORT", "/dev/ttyUSB0"),
    )
    parser.add_argument("--baud", type=int, default=int(os.environ.get("SERIAL_BAUD", "115200")))
    parser.add_argument("--timeout", type=float, default=900, help="Calibration timeout in seconds.")
    parser.add_argument(
        "--minimum-state",
        type=int,
        choices=(2, 3),
        default=2,
        help="Required CalState. 2 is fully calibrated; 3 also requires high-precision heading.",
    )
    parser.add_argument("--clear", action="store_true", help="Clear existing calibration before starting.")
    parser.add_argument("--no-save", action="store_true", help="Do not issue PQTMDRSAVE after completion.")
    parser.add_argument(
        "--hot-start-mode",
        choices=("unchanged", "1", "2"),
        default=os.environ.get("QLM29H_DR_HOT_START_MODE", "2"),
        help="Configure DR hot start. Mode 2 avoids no-signal position output after power-up.",
    )
    parser.add_argument("--status-only", action="store_true", help="Read one calibration status and exit.")
    parser.add_argument(
        "--keep-message-output",
        action="store_true",
        help="Keep PQTMDRCAL output enabled after exit instead of restoring its original rate.",
    )
    parser.add_argument(
        "--service-name",
        default=os.environ.get("QLM29H_SERVICE_NAME", "qlm29h-nmea-unified.service"),
        help="Refuse to run while this systemd service is active.",
    )
    parser.add_argument(
        "--skip-service-check",
        action="store_true",
        help="Skip the systemd service conflict check.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.hot_start_mode not in ("unchanged", "1", "2"):
        print("[ERROR] QLM29H_DR_HOT_START_MODE must be unchanged, 1, or 2", file=sys.stderr)
        return 1
    if args.timeout <= 0:
        print("[ERROR] --timeout must be greater than zero", file=sys.stderr)
        return 1
    if not args.skip_service_check and service_is_active(args.service_name):
        print(
            f"[ERROR] {args.service_name} is active and may be using the serial port.\n"
            f"        Stop it first: sudo systemctl stop {args.service_name}",
            file=sys.stderr,
        )
        return 4

    print(f"[MAIN] Opening {args.serial_port} @ {args.baud}", flush=True)
    try:
        receiver = serial.Serial(args.serial_port, args.baud, timeout=0.2, exclusive=True)
    except (OSError, serial.SerialException) as exc:
        print(f"[ERROR] Cannot open serial port: {exc}", file=sys.stderr)
        return 1

    original_rate: int | None = None
    try:
        receiver.reset_input_buffer()
        original_rate = get_calibration_message_rate(receiver)
        print(f"[MAIN] Original PQTMDRCAL output rate: {original_rate}", flush=True)
        if original_rate != 1:
            set_calibration_message_rate(receiver, 1)
            print("[MAIN] Enabled PQTMDRCAL output once per position fix", flush=True)

        if args.status_only:
            status = read_calibration_status(receiver, 5)
            if status is None:
                print("[ERROR] No PQTMDRCAL status received", file=sys.stderr)
                return 2
            print_status(status)
            return 0

        enable_dr(receiver)
        print("[MAIN] DR enabled", flush=True)
        if args.hot_start_mode != "unchanged":
            set_dr_hot_start(receiver, int(args.hot_start_mode))
            print(f"[MAIN] DR hot start mode set to {args.hot_start_mode}", flush=True)
        if args.clear:
            clear_calibration(receiver)
            print("[MAIN] Existing DR calibration cleared", flush=True)

        print(
            "[DRIVE] Keep the receiver fixed to the vehicle. Drive under clear sky at more than "
            "2 m/s (7.2 km/h) and make 3 or 4 turns.",
            flush=True,
        )
        completed = monitor_calibration(receiver, args.timeout, args.minimum_state)
        if completed is None:
            print(f"[ERROR] Calibration did not reach CalState={args.minimum_state} before timeout", file=sys.stderr)
            return 2

        if not args.no_save:
            save_calibration(receiver)
            print("[MAIN] Calibration data saved with PQTMDRSAVE", flush=True)
        print("[MAIN] Driving calibration completed", flush=True)
        return 0
    except KeyboardInterrupt:
        print("\n[MAIN] Calibration interrupted", file=sys.stderr)
        return 130
    except ReceiverCommandError as exc:
        print(f"[ERROR] Receiver command failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, serial.SerialException) as exc:
        print(f"[ERROR] Serial communication failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if original_rate is not None and not args.keep_message_output and original_rate != 1:
            try:
                set_calibration_message_rate(receiver, original_rate)
                print(f"[MAIN] Restored PQTMDRCAL output rate to {original_rate}", flush=True)
            except (ReceiverCommandError, OSError, serial.SerialException) as exc:
                print(f"[WARN] Failed to restore PQTMDRCAL output rate: {exc}", file=sys.stderr)
        receiver.close()


if __name__ == "__main__":
    sys.exit(main())
