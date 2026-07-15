import copy
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock


sys.modules.setdefault("requests", types.SimpleNamespace(post=None))
sys.modules.setdefault("serial", types.SimpleNamespace(Serial=None, SerialException=Exception))

MODULE_PATH = pathlib.Path(__file__).parents[1] / "rtk_nmea_unified.py"
SPEC = importlib.util.spec_from_file_location("rtk_nmea_unified", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def sample_nmea(sentence_id, message_type, fields):
    return {
        "raw": f"${sentence_id},raw*00",
        "sentence_id": sentence_id,
        "talker": sentence_id[:2],
        "message_type": message_type,
        "checksum": "00",
        "checksum_valid": True,
        "raw_fields": ["raw", "values"],
        "fields": fields,
        "received_at": "2026-07-15T09:00:00+00:00",
    }


def sample_payload():
    return {
        "source": "qlm29h_nmea",
        "serial_port": "/dev/ttyUSB0",
        "sent_at": "2026-07-15T09:00:05+00:00",
        "window": {
            "started_at": "2026-07-15T09:00:00+00:00",
            "ended_at": "2026-07-15T09:00:05+00:00",
            "duration_sec": 5.0,
        },
        "sentence_counts": {"GNGGA": 5, "GNRMC": 5, "GPGSV": 12},
        "nmea": {
            "GNGGA": sample_nmea(
                "GNGGA",
                "GGA",
                {
                    "utc_time_raw": "090005.00",
                    "utc_time": "09:00:05.00Z",
                    "latitude_raw": "4303.00",
                    "latitude": 43.05,
                    "longitude": 141.34,
                    "fix_quality": 4,
                    "fix_quality_label": "Fixed RTK",
                },
            ),
            "GNRMC": sample_nmea(
                "GNRMC",
                "RMC",
                {"date": "2026-07-15", "speed_kmh": 1.2, "course_degrees": 90.0},
            ),
            "GPGSV": [
                sample_nmea(
                    "GPGSV",
                    "GSV",
                    {
                        "total_messages": 1,
                        "raw_field_01": "1",
                        "satellites": [{"satellite_id": "01", "snr_dbhz": 40}],
                    },
                )
            ],
        },
        "latest_position": {
            "lat": 43.05,
            "lon": 141.34,
            "quality": 4,
            "quality_label": "Fixed RTK",
        },
        "lat": 43.05,
        "lon": 141.34,
        "quality": 4,
        "quality_label": "Fixed RTK",
    }


class PayloadControlTests(unittest.TestCase):
    def test_full_preset_is_backward_compatible(self):
        payload = sample_payload()
        config = MODULE.normalize_payload_control({}, 5)
        self.assertEqual(MODULE.apply_payload_control(payload, config), payload)

    def test_compact_preset_removes_raw_and_repeated_metadata(self):
        config = MODULE.normalize_payload_control({"preset": "compact"}, 5)
        filtered = MODULE.apply_payload_control(sample_payload(), config)
        gga = filtered["nmea"]["GNGGA"]
        self.assertEqual(set(gga), {"fields"})
        self.assertNotIn("utc_time_raw", gga["fields"])
        self.assertNotIn("latitude_raw", gga["fields"])
        self.assertIn("latitude", gga["fields"])
        self.assertNotIn("raw_field_01", filtered["nmea"]["GPGSV"][0]["fields"])

    def test_position_preset_keeps_only_position_summary(self):
        config = MODULE.normalize_payload_control({"preset": "position", "interval_sec": 30}, 5)
        filtered = MODULE.apply_payload_control(sample_payload(), config)
        self.assertNotIn("nmea", filtered)
        self.assertNotIn("sentence_counts", filtered)
        self.assertEqual(filtered["latest_position"]["quality"], 4)
        self.assertEqual(filtered["lat"], 43.05)
        self.assertEqual(config["interval_sec"], 30.0)

    def test_custom_selection_and_field_allowlist(self):
        config = MODULE.normalize_payload_control(
            {
                "preset": "custom",
                "include_sentences": ["GGA", "GSV"],
                "include_raw_sentence": False,
                "include_raw_fields": False,
                "include_nmea_metadata": False,
                "include_satellite_details": False,
                "field_allowlist": {"GGA": ["latitude", "longitude"], "GSV": ["total_messages"]},
            },
            5,
        )
        filtered = MODULE.apply_payload_control(sample_payload(), config)
        self.assertEqual(set(filtered["nmea"]), {"GNGGA", "GPGSV"})
        self.assertEqual(filtered["sentence_counts"], {"GNGGA": 5, "GPGSV": 12})
        self.assertEqual(filtered["nmea"]["GNGGA"], {"fields": {"latitude": 43.05, "longitude": 141.34}})
        self.assertEqual(filtered["nmea"]["GPGSV"][0], {"fields": {"total_messages": 1}})

    def test_invalid_configuration_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown payload control keys"):
            MODULE.normalize_payload_control({"intervl_sec": 10}, 5)
        with self.assertRaisesRegex(ValueError, "greater than 0"):
            MODULE.normalize_payload_control({"interval_sec": 0}, 5)

    def test_all_example_configurations_are_valid(self):
        config_dir = MODULE_PATH.parent / "config"
        for path in config_dir.glob("payload-control.*.json"):
            if path.name.endswith("schema.json"):
                continue
            with self.subTest(path=path.name):
                raw = json.loads(path.read_text(encoding="utf-8"))
                MODULE.normalize_payload_control(raw, 5)

    def test_file_control_hot_reload_and_invalid_file_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "payload-control.json"
            path.write_text(json.dumps({"preset": "position", "interval_sec": 30}), encoding="utf-8")
            control = MODULE.PayloadControl(str(path), 5)
            self.assertTrue(control.refresh())
            self.assertEqual(control.config["preset"], "position")

            previous = copy.deepcopy(control.config)
            path.write_text("{invalid-json", encoding="utf-8")
            self.assertFalse(control.refresh())
            self.assertEqual(control.config, previous)

    def test_transmission_stop_pauses_existing_spool(self):
        with tempfile.TemporaryDirectory() as directory:
            args = types.SimpleNamespace(
                spool_dir=directory,
                post_retry_delay=0.01,
                flush_burst=1,
                endpoint="http://example.invalid",
                post_timeout=1,
                max_posts=1,
            )
            MODULE.spool_payload(
                pathlib.Path(directory),
                {"sent_at": "2026-07-15T09:00:00+00:00", "sentence_counts": {"GNGGA": 1}},
                10,
                0,
            )
            wake = threading.Event()
            enabled = threading.Event()
            MODULE.running = True
            try:
                with mock.patch.object(MODULE, "post_payload", return_value=201) as post:
                    worker = threading.Thread(target=MODULE.unified_worker, args=(args, wake, enabled))
                    worker.start()
                    time.sleep(0.05)
                    self.assertFalse(post.called)
                    enabled.set()
                    wake.set()
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
                    post.assert_called_once()
            finally:
                MODULE.running = True


if __name__ == "__main__":
    unittest.main()
