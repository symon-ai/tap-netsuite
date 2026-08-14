import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "tap_netsuite" / "error_file.py"
MODULE_SPEC = importlib.util.spec_from_file_location("tap_netsuite_error_file", MODULE_PATH)
ERROR_FILE_MODULE = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(ERROR_FILE_MODULE)
write_error_info = ERROR_FILE_MODULE.write_error_info


class WriteErrorInfoTest(unittest.TestCase):
    def test_writes_error_file_within_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            previous_directory = os.getcwd()
            os.chdir(temporary_directory)
            try:
                Path("errors").mkdir()
                write_error_info("errors/tapError.json", {"message": "failed"})

                error_file = Path("errors/tapError.json")
                self.assertEqual({"message": "failed"}, json.loads(error_file.read_text(encoding="utf-8")))
                self.assertEqual(0o600, error_file.stat().st_mode & 0o777)
            finally:
                os.chdir(previous_directory)

    def test_rejects_path_outside_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            working_directory = Path(temporary_directory) / "working"
            working_directory.mkdir()
            outside_file = Path(temporary_directory) / "tapError.json"
            previous_directory = os.getcwd()
            os.chdir(working_directory)
            try:
                with self.assertRaisesRegex(ValueError, "within the working directory"):
                    write_error_info(outside_file, {"message": "failed"})
            finally:
                os.chdir(previous_directory)

            self.assertFalse(outside_file.exists())

    def test_rejects_parent_directory_traversal(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            working_directory = Path(temporary_directory) / "working"
            working_directory.mkdir()
            outside_file = Path(temporary_directory) / "tapError.json"
            outside_file.write_text("unchanged", encoding="utf-8")
            previous_directory = os.getcwd()
            os.chdir(working_directory)
            try:
                with self.assertRaisesRegex(ValueError, "within the working directory"):
                    write_error_info("../tapError.json", {"message": "failed"})
            finally:
                os.chdir(previous_directory)

            self.assertEqual("unchanged", outside_file.read_text(encoding="utf-8"))

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW is not available")
    def test_rejects_symbolic_link_destination(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            working_directory = Path(temporary_directory) / "working"
            working_directory.mkdir()
            outside_file = Path(temporary_directory) / "outside.json"
            outside_file.write_text("unchanged", encoding="utf-8")
            (working_directory / "tapError.json").symlink_to(outside_file)
            previous_directory = os.getcwd()
            os.chdir(working_directory)
            try:
                with self.assertRaises(OSError):
                    write_error_info("tapError.json", {"message": "failed"})
            finally:
                os.chdir(previous_directory)

            self.assertEqual("unchanged", outside_file.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
