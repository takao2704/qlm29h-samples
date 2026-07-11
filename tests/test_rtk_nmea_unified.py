import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import threading
import time
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rtk_nmea_unified", ROOT / "rtk_nmea_unified.py")
rtk_nmea_unified = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(rtk_nmea_unified)


def payload(index: int, extra: str = "") -> dict:
    return {
        "sent_at": f"2026-07-11T00:00:{index:02d}+00:00",
        "sentence_counts": {"GNGGA": 1},
        "extra": extra,
    }


def quiet_spool(spool_dir: pathlib.Path, queued_payload: dict, max_files: int, max_bytes: int) -> pathlib.Path | None:
    with contextlib.redirect_stdout(io.StringIO()):
        return rtk_nmea_unified.spool_payload(spool_dir, queued_payload, max_files, max_bytes)


class SpoolPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        rtk_nmea_unified.running = True
        rtk_nmea_unified.fatal_serial_error.clear()

    def tearDown(self) -> None:
        rtk_nmea_unified.running = False

    def test_max_files_prunes_oldest_before_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = pathlib.Path(tmp)
            for index in range(3):
                self.assertIsNotNone(quiet_spool(spool_dir, payload(index), 2, 0))

            remaining = [
                json.loads(path.read_text(encoding="utf-8"))["sent_at"]
                for path in rtk_nmea_unified.spooled_payloads(spool_dir)
            ]
            self.assertEqual(
                remaining,
                ["2026-07-11T00:00:01+00:00", "2026-07-11T00:00:02+00:00"],
            )

    def test_max_bytes_prunes_until_total_size_fits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = pathlib.Path(tmp)
            for index in range(3):
                self.assertIsNotNone(quiet_spool(spool_dir, payload(index, "x" * 120), 0, 450))

            total_bytes = sum(path.stat().st_size for path in rtk_nmea_unified.spooled_payloads(spool_dir))
            self.assertLessEqual(total_bytes, 450)

    def test_oversized_payload_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = pathlib.Path(tmp)
            self.assertIsNone(quiet_spool(spool_dir, payload(1, "x" * 1000), 10, 100))
            self.assertEqual(list(spool_dir.iterdir()), [])

    def test_write_failure_drops_oldest_and_retries_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = pathlib.Path(tmp)
            self.assertIsNotNone(quiet_spool(spool_dir, payload(1), 2, 0))
            original_write_payload_file = rtk_nmea_unified.write_payload_file
            calls = 0

            def flaky_write(tmp_path: pathlib.Path, final_path: pathlib.Path, payload_bytes: bytes) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError("simulated write failure")
                original_write_payload_file(tmp_path, final_path, payload_bytes)

            rtk_nmea_unified.write_payload_file = flaky_write
            try:
                self.assertIsNotNone(quiet_spool(spool_dir, payload(2), 2, 0))
            finally:
                rtk_nmea_unified.write_payload_file = original_write_payload_file

            self.assertEqual(calls, 2)
            remaining = [
                json.loads(path.read_text(encoding="utf-8"))["sent_at"]
                for path in rtk_nmea_unified.spooled_payloads(spool_dir)
            ]
            self.assertEqual(remaining, ["2026-07-11T00:00:02+00:00"])


class UnifiedWorkerTest(unittest.TestCase):
    def tearDown(self) -> None:
        rtk_nmea_unified.running = False

    def test_post_retry_delay_is_not_shortened_by_wake_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spool_dir = pathlib.Path(tmp)
            self.assertIsNotNone(quiet_spool(spool_dir, payload(1), 10, 0))
            attempts = []
            original_post_payload = rtk_nmea_unified.post_payload

            def failing_post(endpoint: str, queued: dict, timeout: float) -> int:
                attempts.append(time.monotonic())
                raise RuntimeError("simulated outage")

            rtk_nmea_unified.post_payload = failing_post
            rtk_nmea_unified.running = True
            args = types.SimpleNamespace(
                spool_dir=str(spool_dir),
                flush_burst=1,
                endpoint="http://example.invalid",
                post_timeout=0.01,
                post_retry_delay=0.4,
                max_posts=0,
            )
            wake_event = threading.Event()
            worker = threading.Thread(target=rtk_nmea_unified.unified_worker, args=(args, wake_event), daemon=True)
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    worker.start()
                    deadline = time.monotonic() + 0.3
                    while time.monotonic() < deadline:
                        wake_event.set()
                        time.sleep(0.03)
                finally:
                    rtk_nmea_unified.running = False
                    rtk_nmea_unified.post_payload = original_post_payload
                    wake_event.set()
                    worker.join(timeout=1)

            self.assertEqual(len(attempts), 1)


if __name__ == "__main__":
    unittest.main()
