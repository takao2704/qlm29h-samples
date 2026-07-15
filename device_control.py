#!/usr/bin/env python3
"""Watch a local JSON command interface and orchestrate QLM29H device actions."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import selectors
import signal
import subprocess
import sys
import time
from typing import Any


DEFAULT_CONFIG_DIR = "/home/pi/.config/qlm29h"
DEFAULT_COMMAND_PATH = f"{DEFAULT_CONFIG_DIR}/device-command.json"
DEFAULT_STATUS_PATH = f"{DEFAULT_CONFIG_DIR}/device-status.json"
DEFAULT_PAYLOAD_CONTROL_PATH = f"{DEFAULT_CONFIG_DIR}/payload-control.json"
DEFAULT_SENDER_SERVICE = "qlm29h-nmea-unified.service"
COMMAND_ACTIONS = {
    "transmission_start",
    "transmission_stop",
    "payload_config_update",
    "dr_alignment_start",
    "dr_alignment_cancel",
}
CALIBRATION_STATUS_RE = re.compile(r"CalState=(\d+).*NavType=(\d+)")

running = True


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def load_json_object(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _reject_unknown(value: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"unknown {name} keys: {', '.join(unknown)}")


def normalize_device_command(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("device command must be a JSON object")
    _reject_unknown(raw, {"version", "request_id", "action", "parameters"}, "device command")

    if raw.get("version", 1) != 1:
        raise ValueError("unsupported device command version")
    request_id = raw.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
        raise ValueError("request_id must be a non-empty string of at most 128 characters")
    action = raw.get("action")
    if action not in COMMAND_ACTIONS:
        raise ValueError(f"action must be one of: {', '.join(sorted(COMMAND_ACTIONS))}")
    parameters = raw.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ValueError("parameters must be an object")

    if action in {"transmission_start", "transmission_stop"}:
        _reject_unknown(parameters, set(), f"{action} parameters")
        normalized_parameters = {}
    elif action == "payload_config_update":
        from rtk_nmea_unified import normalize_payload_control

        _reject_unknown(parameters, {"configuration"}, "payload_config_update parameters")
        configuration = parameters.get("configuration")
        if not isinstance(configuration, dict):
            raise ValueError("payload_config_update requires a configuration object")
        normalized_configuration = normalize_payload_control(configuration, default_interval=5.0)
        if "enabled" not in configuration:
            normalized_configuration.pop("enabled", None)
        normalized_parameters = {"configuration": normalized_configuration}
    elif action == "dr_alignment_cancel":
        _reject_unknown(parameters, {"target_request_id"}, "dr_alignment_cancel parameters")
        target = parameters.get("target_request_id")
        if not isinstance(target, str) or not target:
            raise ValueError("dr_alignment_cancel requires target_request_id")
        normalized_parameters = {"target_request_id": target}
    else:
        allowed = {
            "timeout_sec",
            "minimum_state",
            "clear_existing",
            "hot_start_mode",
            "save_on_complete",
            "keep_message_output",
        }
        _reject_unknown(parameters, allowed, "dr_alignment_start parameters")
        timeout = parameters.get("timeout_sec", 900)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 7200:
            raise ValueError("timeout_sec must be between 1 and 7200")
        minimum_state = parameters.get("minimum_state", 2)
        if minimum_state not in (2, 3):
            raise ValueError("minimum_state must be 2 or 3")
        hot_start_mode = str(parameters.get("hot_start_mode", "2"))
        if hot_start_mode not in ("1", "2", "unchanged"):
            raise ValueError("hot_start_mode must be 1, 2, or unchanged")
        normalized_parameters = {
            "timeout_sec": float(timeout),
            "minimum_state": minimum_state,
            "clear_existing": parameters.get("clear_existing", False),
            "hot_start_mode": hot_start_mode,
            "save_on_complete": parameters.get("save_on_complete", True),
            "keep_message_output": parameters.get("keep_message_output", False),
        }
        for key in ("clear_existing", "save_on_complete", "keep_message_output"):
            if not isinstance(normalized_parameters[key], bool):
                raise ValueError(f"{key} must be true or false")

    return {
        "version": 1,
        "request_id": request_id,
        "action": action,
        "parameters": normalized_parameters,
    }


def status_for(command: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    value = {
        "version": 1,
        "request_id": command["request_id"],
        "action": command["action"],
        "status": status,
        "updated_at": utc_now(),
    }
    value.update(extra)
    return value


def set_transmission_enabled(path: pathlib.Path, enabled: bool) -> dict[str, Any]:
    try:
        config = load_json_object(path)
    except FileNotFoundError:
        config = {"version": 1, "preset": "full"}
    if config.get("version", 1) != 1:
        raise ValueError("unsupported payload control version")
    config["version"] = 1
    config["enabled"] = enabled
    atomic_write_json(path, config)
    return config


def set_payload_configuration(path: pathlib.Path, configuration: dict[str, Any]) -> dict[str, Any]:
    try:
        current = load_json_object(path)
    except FileNotFoundError:
        current = {}
    value = dict(configuration)
    if "enabled" not in value:
        value["enabled"] = current.get("enabled", True)
    atomic_write_json(path, value)
    return value


def service_is_active(service_name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", service_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def set_service_state(service_name: str, action: str) -> tuple[bool, str]:
    command = ["systemctl", action, service_name]
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return result.returncode == 0, result.stdout.strip()


def build_calibration_command(args: argparse.Namespace, command: dict[str, Any]) -> list[str]:
    parameters = command["parameters"]
    executable = pathlib.Path(args.calibration_script)
    result = [
        args.python,
        str(executable),
        "--serial-port",
        args.serial_port,
        "--baud",
        str(args.baud),
        "--timeout",
        f"{parameters['timeout_sec']:g}",
        "--minimum-state",
        str(parameters["minimum_state"]),
        "--hot-start-mode",
        parameters["hot_start_mode"],
        "--skip-service-check",
    ]
    if parameters["clear_existing"]:
        result.append("--clear")
    if not parameters["save_on_complete"]:
        result.append("--no-save")
    if parameters["keep_message_output"]:
        result.append("--keep-message-output")
    return result


def parse_calibration_progress(line: str) -> dict[str, int] | None:
    match = CALIBRATION_STATUS_RE.search(line)
    if match is None:
        return None
    return {"calibration_state": int(match.group(1)), "navigation_type": int(match.group(2))}


class DeviceController:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.command_path = pathlib.Path(args.command_path)
        self.status_path = pathlib.Path(args.status_path)
        self.payload_control_path = pathlib.Path(args.payload_control)
        self.last_signature: tuple[int, int] | None | object = object()
        self.last_request_id: str | None = None
        self.deferred_command: dict[str, Any] | None = None
        self._recover_interrupted_status()

    def _recover_interrupted_status(self) -> None:
        try:
            status = load_json_object(self.status_path)
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
            return
        request_id = status.get("request_id")
        if isinstance(request_id, str):
            self.last_request_id = request_id
        if status.get("status") not in {"accepted", "stopping_sender", "running", "cancelling"}:
            return
        restart = bool(status.get("sender_was_active"))
        restart_ok = True
        restart_message = ""
        if restart:
            restart_ok, restart_message = set_service_state(self.args.sender_service, "start")
        status.update(
            {
                "status": "failed",
                "updated_at": utc_now(),
                "error": "control service restarted while the command was running",
                "sender_restart_ok": restart_ok,
            }
        )
        if restart_message:
            status["sender_restart_message"] = restart_message
        atomic_write_json(self.status_path, status)

    def _read_changed_command(self, *, include_deferred: bool = True) -> dict[str, Any] | None:
        if include_deferred and self.deferred_command is not None:
            command = self.deferred_command
            self.deferred_command = None
            return command
        try:
            stat = self.command_path.stat()
            signature: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            signature = None
        if signature == self.last_signature:
            return None
        self.last_signature = signature
        if signature is None:
            return None
        raw: dict[str, Any] = {}
        try:
            raw = load_json_object(self.command_path)
            command = normalize_device_command(raw)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            invalid = {
                "version": 1,
                "request_id": None,
                "action": None,
                "status": "invalid",
                "updated_at": utc_now(),
                "error": str(exc),
            }
            raw_request_id = raw.get("request_id")
            if isinstance(raw_request_id, str):
                invalid["request_id"] = raw_request_id
            atomic_write_json(self.status_path, invalid)
            return None
        if command["request_id"] == self.last_request_id:
            return None
        return command

    def run(self) -> int:
        print(f"[CONTROL] Watching {self.command_path}", flush=True)
        while running:
            command = self._read_changed_command()
            if command is None:
                time.sleep(self.args.poll_interval)
                continue
            self.last_request_id = command["request_id"]
            atomic_write_json(self.status_path, status_for(command, "accepted"))
            action = command["action"]
            if action in {"transmission_start", "transmission_stop"}:
                self._handle_transmission(command, action == "transmission_start")
            elif action == "payload_config_update":
                self._handle_payload_configuration(command)
            elif action == "dr_alignment_start":
                self._handle_alignment(command)
            else:
                atomic_write_json(
                    self.status_path,
                    status_for(command, "failed", error="no DR alignment is currently running"),
                )
        return 0

    def _handle_transmission(self, command: dict[str, Any], enabled: bool) -> None:
        try:
            config = set_transmission_enabled(self.payload_control_path, enabled)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            atomic_write_json(self.status_path, status_for(command, "failed", error=str(exc)))
            return
        atomic_write_json(
            self.status_path,
            status_for(
                command,
                "completed",
                transmission_enabled=enabled,
                payload_preset=config.get("preset", "full"),
            ),
        )

    def _handle_payload_configuration(self, command: dict[str, Any]) -> None:
        try:
            config = set_payload_configuration(
                self.payload_control_path,
                command["parameters"]["configuration"],
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            atomic_write_json(self.status_path, status_for(command, "failed", error=str(exc)))
            return
        atomic_write_json(
            self.status_path,
            status_for(
                command,
                "completed",
                transmission_enabled=config["enabled"],
                payload_preset=config["preset"],
                interval_sec=config["interval_sec"],
                include_sentences=config["include_sentences"],
            ),
        )

    def _handle_alignment(self, command: dict[str, Any]) -> None:
        sender_was_active = service_is_active(self.args.sender_service)
        atomic_write_json(
            self.status_path,
            status_for(command, "accepted", sender_was_active=sender_was_active),
        )

        if sender_was_active:
            atomic_write_json(
                self.status_path,
                status_for(command, "stopping_sender", sender_was_active=True),
            )
            stopped, message = set_service_state(self.args.sender_service, "stop")
            if not stopped:
                atomic_write_json(
                    self.status_path,
                    status_for(
                        command,
                        "failed",
                        sender_was_active=True,
                        error=f"failed to stop {self.args.sender_service}",
                        service_message=message,
                    ),
                )
                return

        calibration_command = build_calibration_command(self.args, command)
        process: subprocess.Popen[str] | None = None
        selector = selectors.DefaultSelector()
        cancel_command: dict[str, Any] | None = None
        interrupt_requested_at: float | None = None
        progress: dict[str, int] = {}
        output_tail: list[str] = []
        started_at = utc_now()
        try:
            process = subprocess.Popen(
                calibration_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            selector.register(process.stdout, selectors.EVENT_READ)
            atomic_write_json(
                self.status_path,
                status_for(
                    command,
                    "running",
                    sender_was_active=sender_was_active,
                    started_at=started_at,
                    instructions="Drive over 2 m/s under clear sky and make 3 or 4 turns.",
                    **progress,
                ),
            )

            while process.poll() is None:
                if not running:
                    if interrupt_requested_at is None:
                        process.send_signal(signal.SIGINT)
                        interrupt_requested_at = time.monotonic()
                if interrupt_requested_at is not None and time.monotonic() - interrupt_requested_at > 10:
                    process.kill()
                for key, _ in selector.select(timeout=self.args.poll_interval):
                    line = key.fileobj.readline()
                    if not line:
                        continue
                    line = line.rstrip()
                    print(f"[ALIGN] {line}", flush=True)
                    output_tail.append(line)
                    output_tail = output_tail[-20:]
                    parsed = parse_calibration_progress(line)
                    if parsed is not None and parsed != progress:
                        progress = parsed
                        atomic_write_json(
                            self.status_path,
                            status_for(
                                command,
                                "running",
                                sender_was_active=sender_was_active,
                                started_at=started_at,
                                **progress,
                            ),
                        )

                candidate = self._read_changed_command(include_deferred=False)
                if candidate is None:
                    continue
                if (
                    candidate["action"] == "dr_alignment_cancel"
                    and candidate["parameters"]["target_request_id"] == command["request_id"]
                ):
                    cancel_command = candidate
                    self.last_request_id = candidate["request_id"]
                    atomic_write_json(
                        self.status_path,
                        status_for(
                            candidate,
                            "cancelling",
                            target_request_id=command["request_id"],
                            sender_was_active=sender_was_active,
                        ),
                    )
                    process.send_signal(signal.SIGINT)
                    interrupt_requested_at = time.monotonic()
                    self.deferred_command = None
                else:
                    self.deferred_command = candidate

            return_code = process.wait()
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                print(f"[ALIGN] {line}", flush=True)
                output_tail.append(line)
                output_tail = output_tail[-20:]
                parsed = parse_calibration_progress(line)
                if parsed is not None:
                    progress = parsed
        except (OSError, subprocess.SubprocessError) as exc:
            return_code = None
            output_tail.append(str(exc))
        finally:
            selector.close()
            if process is not None and process.stdout is not None:
                process.stdout.close()
            restart_ok = True
            restart_message = ""
            if sender_was_active:
                restart_ok, restart_message = set_service_state(self.args.sender_service, "start")

        tail = output_tail[-10:]
        if cancel_command is not None:
            final = status_for(
                cancel_command,
                "cancelled",
                target_request_id=command["request_id"],
                sender_restart_ok=restart_ok,
                output_tail=tail,
                **progress,
            )
        elif return_code == 0:
            final = status_for(
                command,
                "completed",
                started_at=started_at,
                completed_at=utc_now(),
                sender_was_active=sender_was_active,
                sender_restart_ok=restart_ok,
                output_tail=tail,
                **progress,
            )
        else:
            final = status_for(
                command,
                "failed",
                started_at=started_at,
                failed_at=utc_now(),
                sender_was_active=sender_was_active,
                sender_restart_ok=restart_ok,
                exit_code=return_code,
                error="DR alignment did not complete",
                output_tail=tail,
                **progress,
            )
        if restart_message:
            final["sender_restart_message"] = restart_message
        atomic_write_json(self.status_path, final)


def handle_signal(_signum: int, _frame: Any) -> None:
    global running
    running = False


def parse_args() -> argparse.Namespace:
    script_dir = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--command-path", default=os.environ.get("QLM29H_COMMAND_PATH", DEFAULT_COMMAND_PATH))
    parser.add_argument("--status-path", default=os.environ.get("QLM29H_STATUS_PATH", DEFAULT_STATUS_PATH))
    parser.add_argument(
        "--payload-control",
        default=os.environ.get("UNIFIED_PAYLOAD_CONTROL", DEFAULT_PAYLOAD_CONTROL_PATH),
    )
    parser.add_argument("--sender-service", default=os.environ.get("QLM29H_SERVICE_NAME", DEFAULT_SENDER_SERVICE))
    parser.add_argument("--calibration-script", default=str(script_dir / "dr_calibrate.py"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument(
        "--serial-port",
        default=os.environ.get("SERIAL_PORT", "/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0"),
    )
    parser.add_argument("--baud", type=int, default=int(os.environ.get("SERIAL_BAUD", "115200")))
    parser.add_argument("--poll-interval", type=float, default=0.5)
    args = parser.parse_args()
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be greater than zero")
    return args


def main() -> int:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    return DeviceController(parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())
