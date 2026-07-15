import argparse
import importlib.util
import json
import pathlib
import stat
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "device_control.py"
SPEC = importlib.util.spec_from_file_location("device_control", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def controller_args(directory, calibration_script=None):
    root = pathlib.Path(directory)
    return argparse.Namespace(
        command_path=str(root / "device-command.json"),
        status_path=str(root / "device-status.json"),
        payload_control=str(root / "payload-control.json"),
        sender_service="qlm29h-nmea-unified.service",
        calibration_script=str(calibration_script or root / "fake_calibrate.py"),
        python=sys.executable,
        serial_port="/dev/fake-serial",
        baud=115200,
        poll_interval=0.01,
    )


class DeviceControlTests(unittest.TestCase):
    def test_alignment_defaults_and_validation(self):
        command = MODULE.normalize_device_command(
            {
                "version": 1,
                "request_id": "alignment-1",
                "action": "dr_alignment_start",
            }
        )
        self.assertEqual(command["parameters"]["timeout_sec"], 900.0)
        self.assertEqual(command["parameters"]["minimum_state"], 2)
        self.assertFalse(command["parameters"]["clear_existing"])
        self.assertEqual(command["parameters"]["hot_start_mode"], "2")
        self.assertTrue(command["parameters"]["save_on_complete"])

        with self.assertRaisesRegex(ValueError, "clear_existing"):
            MODULE.normalize_device_command(
                {
                    "request_id": "alignment-2",
                    "action": "dr_alignment_start",
                    "parameters": {"clear_existing": 1},
                }
            )
        with self.assertRaisesRegex(ValueError, "target_request_id"):
            MODULE.normalize_device_command(
                {"request_id": "cancel-1", "action": "dr_alignment_cancel", "parameters": {}}
            )

    def test_transmission_command_updates_existing_payload_control(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "payload-control.json"
            path.write_text(json.dumps({"version": 1, "preset": "compact", "interval_sec": 30}), encoding="utf-8")
            stopped = MODULE.set_transmission_enabled(path, False)
            self.assertFalse(stopped["enabled"])
            self.assertEqual(stopped["preset"], "compact")
            self.assertEqual(stopped["interval_sec"], 30)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            started = MODULE.set_transmission_enabled(path, True)
            self.assertTrue(started["enabled"])
            self.assertEqual(started["preset"], "compact")

    def test_calibration_command_maps_all_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            args = controller_args(directory)
            command = MODULE.normalize_device_command(
                {
                    "request_id": "alignment-3",
                    "action": "dr_alignment_start",
                    "parameters": {
                        "timeout_sec": 600,
                        "minimum_state": 3,
                        "clear_existing": True,
                        "hot_start_mode": "1",
                        "save_on_complete": False,
                        "keep_message_output": True,
                    },
                }
            )
            result = MODULE.build_calibration_command(args, command)
            self.assertIn("--clear", result)
            self.assertIn("--no-save", result)
            self.assertIn("--keep-message-output", result)
            self.assertEqual(result[result.index("--minimum-state") + 1], "3")
            self.assertEqual(result[result.index("--timeout") + 1], "600")

    def test_progress_parser(self):
        self.assertEqual(
            MODULE.parse_calibration_progress(
                "[STATUS   22.1s] CalState=2 (fully calibrated), NavType=3 (GNSS + DR)"
            ),
            {"calibration_state": 2, "navigation_type": 3},
        )
        self.assertIsNone(MODULE.parse_calibration_progress("[MAIN] DR enabled"))

    def test_non_root_service_control_uses_noninteractive_sudo(self):
        result = mock.Mock(returncode=0, stdout="")
        with (
            mock.patch.object(MODULE.os, "geteuid", return_value=1000),
            mock.patch.object(MODULE.subprocess, "run", return_value=result) as run,
        ):
            self.assertEqual(MODULE.set_service_state("sender.service", "stop"), (True, ""))
        self.assertEqual(
            run.call_args.args[0],
            ["sudo", "-n", "systemctl", "stop", "sender.service"],
        )

    def test_same_request_is_not_read_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            args = controller_args(directory)
            command_path = pathlib.Path(args.command_path)
            command_path.write_text(
                json.dumps({"request_id": "stop-1", "action": "transmission_stop"}),
                encoding="utf-8",
            )
            controller = MODULE.DeviceController(args)
            command = controller._read_changed_command()
            self.assertEqual(command["request_id"], "stop-1")
            controller.last_request_id = "stop-1"
            self.assertIsNone(controller._read_changed_command())

    def test_alignment_orchestration_records_completion_and_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            script = pathlib.Path(directory) / "fake_calibrate.py"
            script.write_text(
                "print('[MAIN] DR enabled', flush=True)\n"
                "print('[STATUS 1.0s] CalState=2 (fully calibrated), NavType=3 (GNSS + DR)', flush=True)\n",
                encoding="utf-8",
            )
            args = controller_args(directory, script)
            controller = MODULE.DeviceController(args)
            command = MODULE.normalize_device_command(
                {"request_id": "alignment-4", "action": "dr_alignment_start"}
            )
            with (
                mock.patch.object(MODULE, "service_is_active", return_value=True),
                mock.patch.object(MODULE, "set_service_state", return_value=(True, "")) as service_state,
            ):
                controller._handle_alignment(command)
            status_value = json.loads(pathlib.Path(args.status_path).read_text(encoding="utf-8"))
            self.assertEqual(status_value["status"], "completed")
            self.assertEqual(status_value["calibration_state"], 2)
            self.assertEqual(status_value["navigation_type"], 3)
            self.assertTrue(status_value["sender_restart_ok"])
            self.assertEqual(
                service_state.call_args_list,
                [
                    mock.call("qlm29h-nmea-unified.service", "stop"),
                    mock.call("qlm29h-nmea-unified.service", "start"),
                ],
            )

    def test_alignment_can_be_cancelled_by_target_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            script = pathlib.Path(directory) / "fake_calibrate.py"
            script.write_text(
                "import time\n"
                "print('[MAIN] DR enabled', flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            args = controller_args(directory, script)
            pathlib.Path(args.command_path).write_text(
                json.dumps(
                    {
                        "request_id": "cancel-4",
                        "action": "dr_alignment_cancel",
                        "parameters": {"target_request_id": "alignment-5"},
                    }
                ),
                encoding="utf-8",
            )
            controller = MODULE.DeviceController(args)
            command = MODULE.normalize_device_command(
                {"request_id": "alignment-5", "action": "dr_alignment_start"}
            )
            with mock.patch.object(MODULE, "service_is_active", return_value=False):
                controller._handle_alignment(command)
            status_value = json.loads(pathlib.Path(args.status_path).read_text(encoding="utf-8"))
            self.assertEqual(status_value["request_id"], "cancel-4")
            self.assertEqual(status_value["status"], "cancelled")
            self.assertEqual(status_value["target_request_id"], "alignment-5")

    def test_all_command_examples_are_valid(self):
        config_dir = MODULE_PATH.parent / "config"
        for path in config_dir.glob("device-command.*.json"):
            if path.name.endswith("schema.json"):
                continue
            with self.subTest(path=path.name):
                raw = json.loads(path.read_text(encoding="utf-8"))
                MODULE.normalize_device_command(raw)


if __name__ == "__main__":
    unittest.main()
