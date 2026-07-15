import importlib.util
import json
import pathlib
import tempfile
import unittest


MODULE_PATH = pathlib.Path(__file__).parents[1] / "remote_command_http.py"
SPEC = importlib.util.spec_from_file_location("remote_command_http", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class RemoteCommandApplicationTests(unittest.TestCase):
    def create_app(self, directory):
        root = pathlib.Path(directory)
        return MODULE.RemoteCommandApplication(
            command_path=root / "device-command.json",
            status_path=root / "device-status.json",
            payload_control_path=root / "payload-control.json",
            telemetry_status_path=root / "telemetry-status.json",
            token="test-token",
            allowed_source="100.127.10.16",
            device_id="device-01",
        )

    def test_rejects_wrong_source_and_token(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.create_app(directory)
            status, _ = app.handle("GET", "/v1/status", {}, b"", "192.0.2.10")
            self.assertEqual(status, 403)
            status, _ = app.handle("GET", "/v1/status", {}, b"", "100.127.10.16")
            self.assertEqual(status, 401)

    def test_accepts_valid_command_and_writes_atomic_file(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.create_app(directory)
            command = {
                "version": 1,
                "request_id": "remote-stop-1",
                "action": "transmission_stop",
            }
            status, response = app.handle(
                "POST",
                "/v1/commands",
                {"authorization": "Bearer test-token"},
                json.dumps(command).encode(),
                "100.127.10.16",
            )
            self.assertEqual(status, 202)
            self.assertEqual(response["request_id"], "remote-stop-1")
            written = json.loads((pathlib.Path(directory) / "device-command.json").read_text())
            self.assertEqual(written["action"], "transmission_stop")

    def test_status_includes_payload_selection_and_controller_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "payload-control.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "enabled": False,
                        "preset": "custom",
                        "include_sentences": ["GGA", "RMC"],
                    }
                )
            )
            (root / "device-status.json").write_text(
                json.dumps({"request_id": "stop-1", "status": "completed"})
            )
            (root / "telemetry-status.json").write_text(
                json.dumps(
                    {
                        "latest_data_received_at": "2026-07-16T00:00:00+00:00",
                        "latest_position": {"lat": 35.681236, "lon": 139.767125},
                        "rtk": {"quality": 4, "quality_label": "Fixed RTK"},
                        "ntrip": {"status": "receiving"},
                        "satellites": {"used": 21, "in_view": 34},
                    }
                )
            )
            app = self.create_app(directory)
            status, response = app.handle(
                "GET",
                "/v1/status",
                {"authorization": "Bearer test-token"},
                b"",
                "100.127.10.16",
            )
            self.assertEqual(status, 200)
            self.assertFalse(response["transmission_enabled"])
            self.assertEqual(response["payload_control"]["include_sentences"], ["GGA", "RMC"])
            self.assertEqual(response["control_status"]["status"], "completed")
            self.assertEqual(response["telemetry_status"]["rtk"]["quality"], 4)
            self.assertEqual(response["telemetry_status"]["satellites"]["used"], 21)

    def test_rejects_invalid_command(self):
        with tempfile.TemporaryDirectory() as directory:
            app = self.create_app(directory)
            status, response = app.handle(
                "POST",
                "/v1/commands",
                {"authorization": "Bearer test-token"},
                b'{"request_id":"bad","action":"shell"}',
                "100.127.10.16",
            )
            self.assertEqual(status, 400)
            self.assertIn("action must be one of", response["error"])


if __name__ == "__main__":
    unittest.main()
