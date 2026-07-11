import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dr_calibrate", ROOT / "dr_calibrate.py")
dr_calibrate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = dr_calibrate
SPEC.loader.exec_module(dr_calibrate)


class CommandTest(unittest.TestCase):
    def test_documented_commands_have_expected_checksums(self) -> None:
        self.assertEqual(dr_calibrate.build_command("PQTMCFGDR,W,1"), b"$PQTMCFGDR,W,1*2A\r\n")
        self.assertEqual(dr_calibrate.build_command("PQTMDRSAVE"), b"$PQTMDRSAVE*0F\r\n")
        self.assertEqual(dr_calibrate.build_command("PQTMDRCLR"), b"$PQTMDRCLR*53\r\n")
        self.assertEqual(dr_calibrate.build_command("PQTMCFGDRHOT,W,2"), b"$PQTMCFGDRHOT,W,2*7A\r\n")
        self.assertEqual(
            dr_calibrate.build_command("PQTMCFGMSGRATE,W,PQTMDRCAL,1,1"),
            b"$PQTMCFGMSGRATE,W,PQTMDRCAL,1,1*16\r\n",
        )


class CalibrationStatusTest(unittest.TestCase):
    def test_parse_uncalibrated_gnss_only_status(self) -> None:
        status = dr_calibrate.parse_calibration_status("$PQTMDRCAL,1,0,1*5C")
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.calibration_state, 0)
        self.assertEqual(status.calibration_label, "not calibrated")
        self.assertEqual(status.navigation_type, 1)
        self.assertEqual(status.navigation_label, "GNSS only")

    def test_parse_fully_calibrated_combined_status(self) -> None:
        body = "PQTMDRCAL,1,2,3"
        line = f"${body}*{dr_calibrate.nmea_checksum(body)}"
        status = dr_calibrate.parse_calibration_status(line)
        self.assertIsNotNone(status)
        assert status is not None
        self.assertEqual(status.calibration_state, 2)
        self.assertEqual(status.navigation_type, 3)

    def test_rejects_bad_checksum(self) -> None:
        self.assertIsNone(dr_calibrate.parse_calibration_status("$PQTMDRCAL,1,0,1*00"))

    def test_parse_message_rate(self) -> None:
        self.assertEqual(
            dr_calibrate.parse_message_rate("$PQTMCFGMSGRATE,OK,PQTMDRCAL,0,1*44"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
