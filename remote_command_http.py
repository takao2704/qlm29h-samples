#!/usr/bin/env python3
"""Expose the local JSON device-control interface to SORACOM Remote Command HTTP."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import pathlib
import signal
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from device_control import atomic_write_json, load_json_object, normalize_device_command, utc_now


DEFAULT_CONFIG_DIR = "/home/pi/.config/qlm29h"
DEFAULT_COMMAND_PATH = f"{DEFAULT_CONFIG_DIR}/device-command.json"
DEFAULT_STATUS_PATH = f"{DEFAULT_CONFIG_DIR}/device-status.json"
DEFAULT_PAYLOAD_CONTROL_PATH = f"{DEFAULT_CONFIG_DIR}/payload-control.json"
DEFAULT_REMOTE_COMMAND_SOURCE = "100.127.10.16"
MAX_BODY_BYTES = 64 * 1024


class RemoteCommandApplication:
    def __init__(
        self,
        *,
        command_path: pathlib.Path,
        status_path: pathlib.Path,
        payload_control_path: pathlib.Path,
        token: str,
        allowed_source: str,
        device_id: str,
    ) -> None:
        self.command_path = command_path
        self.status_path = status_path
        self.payload_control_path = payload_control_path
        self.token = token
        self.allowed_source = allowed_source
        self.device_id = device_id

    def handle(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        client_ip: str,
    ) -> tuple[int, dict[str, Any]]:
        if self.allowed_source and client_ip != self.allowed_source:
            return HTTPStatus.FORBIDDEN, {"error": "request source is not allowed"}
        supplied = headers.get("authorization", "")
        expected = f"Bearer {self.token}"
        if not self.token or not hmac.compare_digest(supplied, expected):
            return HTTPStatus.UNAUTHORIZED, {"error": "invalid command token"}

        if method == "GET" and path == "/v1/status":
            return HTTPStatus.OK, self._status()
        if method == "POST" and path == "/v1/commands":
            return self._accept_command(body)
        return HTTPStatus.NOT_FOUND, {"error": "not found"}

    def _status(self) -> dict[str, Any]:
        try:
            control_status = load_json_object(self.status_path)
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
            control_status = None
        try:
            payload_control = load_json_object(self.payload_control_path)
        except (FileNotFoundError, OSError, json.JSONDecodeError, ValueError):
            payload_control = {"version": 1, "enabled": True, "preset": "full"}
        return {
            "version": 1,
            "device_id": self.device_id,
            "online": True,
            "observed_at": utc_now(),
            "transmission_enabled": payload_control.get("enabled", True),
            "payload_control": payload_control,
            "control_status": control_status,
        }

    def _accept_command(self, body: bytes) -> tuple[int, dict[str, Any]]:
        if len(body) > MAX_BODY_BYTES:
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body is too large"}
        try:
            raw = json.loads(body.decode("utf-8"))
            command = normalize_device_command(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            return HTTPStatus.BAD_REQUEST, {"error": str(exc)}
        try:
            atomic_write_json(self.command_path, command)
        except OSError as exc:
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
        return HTTPStatus.ACCEPTED, {
            "version": 1,
            "request_id": command["request_id"],
            "action": command["action"],
            "status": "accepted",
            "accepted_at": utc_now(),
        }


class RemoteCommandHandler(BaseHTTPRequestHandler):
    server: "RemoteCommandServer"

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch(b"")

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._write(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request body is too large"})
            return
        self._dispatch(self.rfile.read(length))

    def _dispatch(self, body: bytes) -> None:
        headers = {key.lower(): value for key, value in self.headers.items()}
        status, value = self.server.application.handle(
            self.command,
            self.path.split("?", 1)[0],
            headers,
            body,
            self.client_address[0],
        )
        self._write(status, value)

    def _write(self, status: int, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[REMOTE] {self.client_address[0]} {format % args}", flush=True)


class RemoteCommandServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], application: RemoteCommandApplication) -> None:
        super().__init__(address, RemoteCommandHandler)
        self.application = application


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default=os.environ.get("REMOTE_COMMAND_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("REMOTE_COMMAND_PORT", "8088")))
    parser.add_argument("--command-path", default=os.environ.get("QLM29H_COMMAND_PATH", DEFAULT_COMMAND_PATH))
    parser.add_argument("--status-path", default=os.environ.get("QLM29H_STATUS_PATH", DEFAULT_STATUS_PATH))
    parser.add_argument(
        "--payload-control",
        default=os.environ.get("UNIFIED_PAYLOAD_CONTROL", DEFAULT_PAYLOAD_CONTROL_PATH),
    )
    parser.add_argument(
        "--allowed-source",
        default=os.environ.get("REMOTE_COMMAND_ALLOWED_SOURCE", DEFAULT_REMOTE_COMMAND_SOURCE),
    )
    parser.add_argument("--device-id", default=os.environ.get("QLM29H_DEVICE_ID", "takao_01s_05"))
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    args = parse_args()
    token = os.environ.get("REMOTE_COMMAND_TOKEN", "")
    if not token:
        print("REMOTE_COMMAND_TOKEN is required", file=sys.stderr)
        return 2
    application = RemoteCommandApplication(
        command_path=pathlib.Path(args.command_path),
        status_path=pathlib.Path(args.status_path),
        payload_control_path=pathlib.Path(args.payload_control),
        token=token,
        allowed_source=args.allowed_source,
        device_id=args.device_id,
    )
    server = RemoteCommandServer((args.bind, args.port), application)

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"[REMOTE] Listening on {args.bind}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
