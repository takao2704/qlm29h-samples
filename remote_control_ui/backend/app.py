"""Lambda backend for the QLM29H Remote Control dashboard."""

from __future__ import annotations

import base64
import datetime as dt
import json
import mimetypes
import os
import pathlib
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable


STATIC_DIR = pathlib.Path(__file__).with_name("static")
DEVICE_ACTIONS = {
    "transmission_start",
    "transmission_stop",
    "payload_config_update",
    "dr_alignment_start",
    "dr_alignment_cancel",
}
_soracom_client: "SoracomClient | None" = None


class RemoteControlError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 502, details: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.details = details


@dataclass
class HttpResult:
    status: int
    body: dict[str, Any]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def default_transport(
    url: str,
    method: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
    timeout: float,
) -> HttpResult:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            value = json.loads(raw.decode("utf-8")) if raw else {}
            return HttpResult(response.status, value)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = {"message": raw.decode("utf-8", errors="replace")}
        return HttpResult(exc.code, value)


class SoracomClient:
    def __init__(
        self,
        *,
        endpoint: str,
        sim_id: str,
        device_port: int,
        secret_loader: Callable[[], dict[str, str]],
        transport: Callable[[str, str, dict[str, str], dict[str, Any] | None, float], HttpResult] = default_transport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.sim_id = sim_id
        self.device_port = device_port
        self.secret_loader = secret_loader
        self.transport = transport
        self.clock = clock
        self.api_key = ""
        self.api_token = ""
        self.authenticated_until = 0.0
        self.secret: dict[str, str] | None = None

    def request_device(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_authenticated()
        secret = self._secret()
        remote_request = {
            "method": method,
            "path": path,
            "port": self.device_port,
            "ssl": False,
            "skipVerify": False,
            "headers": {
                "Authorization": f"Bearer {secret['remoteCommandToken']}",
                "Content-Type": "application/json",
            },
        }
        if body is not None:
            remote_request["body"] = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        url = f"{self.endpoint}/v1/sims/{urllib.parse.quote(self.sim_id, safe='')}/downlink/http"
        result = self.transport(
            url,
            "POST",
            {
                "Content-Type": "application/json",
                "X-Soracom-API-Key": self.api_key,
                "X-Soracom-Token": self.api_token,
            },
            remote_request,
            12.0,
        )
        if result.status == 401:
            self.authenticated_until = 0
        if result.status != 200:
            raise RemoteControlError(
                "SORACOM Remote Command request failed",
                status_code=502,
                details={"soracom_status": result.status, "response": result.body},
            )
        return decode_device_response(result.body)

    def _ensure_authenticated(self) -> None:
        if self.api_key and self.api_token and self.clock() < self.authenticated_until:
            return
        secret = self._secret()
        result = self.transport(
            f"{self.endpoint}/v1/auth",
            "POST",
            {"Content-Type": "application/json"},
            {
                "authKeyId": secret["authKeyId"],
                "authKey": secret["authKey"],
                "tokenTimeoutSeconds": 3600,
            },
            8.0,
        )
        if result.status != 200 or not result.body.get("apiKey") or not result.body.get("token"):
            raise RemoteControlError(
                "SORACOM authentication failed",
                status_code=502,
                details={"soracom_status": result.status},
            )
        self.api_key = result.body["apiKey"]
        self.api_token = result.body["token"]
        self.authenticated_until = self.clock() + 3300

    def _secret(self) -> dict[str, str]:
        if self.secret is None:
            self.secret = self.secret_loader()
        return self.secret


def decode_device_response(value: dict[str, Any]) -> dict[str, Any]:
    status = value.get("statusCode")
    if not isinstance(status, int):
        raise RemoteControlError("Remote Command response did not include a device status code")
    raw_body = value.get("body", "")
    if value.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise RemoteControlError("Device returned an unreadable response") from exc
    try:
        body = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError as exc:
        raise RemoteControlError("Device returned non-JSON data", details={"device_status": status}) from exc
    if not 200 <= status < 300:
        raise RemoteControlError(
            body.get("error", "Device rejected the command"),
            status_code=422 if status < 500 else 502,
            details={"device_status": status},
        )
    return body


def load_secret() -> dict[str, str]:
    import boto3

    response = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["SORACOM_SECRET_ARN"])
    value = json.loads(response["SecretString"])
    required = ("authKeyId", "authKey", "remoteCommandToken")
    missing = [key for key in required if not value.get(key)]
    if missing:
        raise RemoteControlError(f"SORACOM secret is missing: {', '.join(missing)}", status_code=500)
    return value


def get_soracom_client() -> SoracomClient:
    global _soracom_client
    if _soracom_client is None:
        _soracom_client = SoracomClient(
            endpoint=os.environ.get("SORACOM_API_ENDPOINT", "https://g.api.soracom.io"),
            sim_id=os.environ["SORACOM_SIM_ID"],
            device_port=int(os.environ.get("DEVICE_HTTP_PORT", "8088")),
            secret_loader=load_secret,
        )
    return _soracom_client


def normalize_command_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RemoteControlError("Request body must be a JSON object", status_code=400)
    unknown = sorted(set(value) - {"action", "parameters"})
    if unknown:
        raise RemoteControlError(f"Unknown request keys: {', '.join(unknown)}", status_code=400)
    action = value.get("action")
    if action not in DEVICE_ACTIONS:
        raise RemoteControlError("Unsupported device action", status_code=400)
    parameters = value.get("parameters", {})
    if not isinstance(parameters, dict):
        raise RemoteControlError("parameters must be an object", status_code=400)
    return {
        "version": 1,
        "request_id": f"web-{uuid.uuid4()}",
        "action": action,
        "parameters": parameters,
    }


def parse_event_body(event: dict[str, Any]) -> Any:
    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RemoteControlError("Request body is not valid JSON", status_code=400) from exc


def save_history(command: dict[str, Any], response: dict[str, Any]) -> None:
    import boto3

    timestamp = utc_now()
    boto3.resource("dynamodb").Table(os.environ["HISTORY_TABLE"]).put_item(
        Item={
            "device_id": os.environ.get("DEVICE_ID", "takao_01s_05"),
            "sort_key": f"{timestamp}#{command['request_id']}",
            "created_at": timestamp,
            "request_id": command["request_id"],
            "action": command["action"],
            "parameters": command["parameters"],
            "result": response,
        }
    )


def load_history(limit: int = 20) -> list[dict[str, Any]]:
    import boto3

    response = boto3.client("dynamodb").query(
        TableName=os.environ["HISTORY_TABLE"],
        KeyConditionExpression="device_id = :device_id",
        ExpressionAttributeValues={":device_id": {"S": os.environ.get("DEVICE_ID", "takao_01s_05")}},
        ScanIndexForward=False,
        Limit=limit,
    )
    deserializer = boto3.dynamodb.types.TypeDeserializer()
    return [{key: deserializer.deserialize(value) for key, value in item.items()} for item in response["Items"]]


def json_response(status: int, value: Any) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
        "body": json.dumps(value, ensure_ascii=False, default=str),
    }


def runtime_config() -> dict[str, str]:
    return {
        "apiBaseUrl": "",
        "deviceId": os.environ.get("DEVICE_ID", "takao_01s_05"),
        "cognitoIssuer": os.environ.get("COGNITO_ISSUER", ""),
        "cognitoDomain": os.environ.get("COGNITO_DOMAIN", ""),
        "cognitoClientId": os.environ.get("COGNITO_CLIENT_ID", ""),
        "mockMode": "false",
    }


def static_response(path: str) -> dict[str, Any]:
    if path == "/runtime-config.json":
        return json_response(200, runtime_config())
    relative = path.lstrip("/") or "index.html"
    candidate = (STATIC_DIR / relative).resolve()
    if STATIC_DIR.resolve() not in candidate.parents or not candidate.is_file():
        candidate = STATIC_DIR / "index.html"
    if not candidate.is_file():
        return json_response(503, {"error": "Dashboard assets have not been built"})
    data = candidate.read_bytes()
    content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    textual = content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "no-cache" if candidate.name == "index.html" else "public, max-age=31536000, immutable",
        },
        "body": data.decode("utf-8") if textual else base64.b64encode(data).decode("ascii"),
        "isBase64Encoded": not textual,
    }


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", "GET")
    path = event.get("rawPath", "/")
    try:
        if method == "GET" and path == "/api/device":
            return json_response(200, get_soracom_client().request_device("GET", "/v1/status"))
        if method == "POST" and path == "/api/commands":
            command = normalize_command_request(parse_event_body(event))
            result = get_soracom_client().request_device("POST", "/v1/commands", command)
            try:
                save_history(command, result)
            except Exception as exc:
                print(f"failed to save command history: {type(exc).__name__}")
            return json_response(202, result)
        if method == "GET" and path == "/api/history":
            return json_response(200, {"items": load_history()})
        if method == "GET":
            return static_response(path)
        return json_response(404, {"error": "not found"})
    except RemoteControlError as exc:
        value: dict[str, Any] = {"error": str(exc)}
        if exc.details is not None:
            value["details"] = exc.details
        return json_response(exc.status_code, value)
    except Exception:
        return json_response(500, {"error": "Unexpected server error"})
