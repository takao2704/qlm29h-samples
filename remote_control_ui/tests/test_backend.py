import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "backend" / "app.py"
SPEC = importlib.util.spec_from_file_location("remote_control_backend", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SoracomClientTests(unittest.TestCase):
    def test_authenticates_and_wraps_device_request(self):
        calls = []

        def transport(url, method, headers, body, timeout):
            calls.append((url, method, headers, body, timeout))
            if url.endswith("/v1/auth"):
                return MODULE.HttpResult(200, {"apiKey": "api-key", "token": "api-token"})
            return MODULE.HttpResult(
                200,
                {
                    "statusCode": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({"online": True}),
                    "isBase64Encoded": False,
                },
            )

        client = MODULE.SoracomClient(
            endpoint="https://g.api.soracom.io",
            sim_id="8942310224000601522",
            device_port=8088,
            secret_loader=lambda: {
                "authKeyId": "keyId-test",
                "authKey": "secret-test",
                "remoteCommandToken": "device-token",
            },
            transport=transport,
            clock=lambda: 100,
        )
        response = client.request_device("GET", "/v1/status")
        self.assertTrue(response["online"])
        self.assertEqual(len(calls), 2)
        downlink = calls[1]
        self.assertEqual(downlink[3]["port"], 8088)
        self.assertEqual(downlink[3]["headers"]["Authorization"], "Bearer device-token")
        self.assertEqual(downlink[2]["X-Soracom-API-Key"], "api-key")

    def test_rejects_device_error_response(self):
        with self.assertRaisesRegex(MODULE.RemoteControlError, "bad command"):
            MODULE.decode_device_response(
                {"statusCode": 400, "body": '{"error":"bad command"}', "isBase64Encoded": False}
            )

    def test_normalizes_allowed_command_only(self):
        command = MODULE.normalize_command_request({"action": "transmission_stop"})
        self.assertEqual(command["action"], "transmission_stop")
        self.assertTrue(command["request_id"].startswith("web-"))
        with self.assertRaisesRegex(MODULE.RemoteControlError, "Unsupported"):
            MODULE.normalize_command_request({"action": "shell"})

    def test_load_history_imports_dynamodb_deserializer_explicitly(self):
        dynamodb = mock.Mock()
        dynamodb.query.return_value = {
            "Items": [
                {
                    "device_id": {"S": "takao_01s_05"},
                    "sort_key": {"S": "2026-07-15T13:31:14+00:00#web-1"},
                    "action": {"S": "transmission_stop"},
                }
            ]
        }

        with mock.patch.dict(MODULE.os.environ, {"HISTORY_TABLE": "test-history"}), \
                mock.patch("boto3.client", return_value=dynamodb):
            history = MODULE.load_history()

        self.assertEqual(history[0]["action"], "transmission_stop")
        dynamodb.query.assert_called_once()


if __name__ == "__main__":
    unittest.main()
