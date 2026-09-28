"""Regression tests for WP-33455 / CWE-73 error_file_path validation.

These use only the standard library (unittest) so they run without the tap's
runtime dependencies installed, and they import the validation helper directly
so the security check is exercised in isolation from the singer machinery.
"""

import importlib.util
import os
import tempfile
import unittest

# Load path_utils directly by file path so the test does not require the tap's
# runtime dependencies (importing the tap_netsuite package would pull in singer).
_PATH_UTILS_PATH = os.path.join(os.path.dirname(__file__), '..', 'tap_netsuite', 'path_utils.py')
_spec = importlib.util.spec_from_file_location('tap_netsuite_path_utils', _PATH_UTILS_PATH)
path_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(path_utils)
validate_error_file_path = path_utils.validate_error_file_path
UnsafePathError = path_utils.UnsafePathError


class ValidateErrorFilePathTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_legitimate_relative_path_is_allowed(self):
        # This mirrors how elt/app import-activity supplies error_file_path:
        # a file underneath the per-import local working directory.
        resolved = validate_error_file_path('tapError.json', base_dir=self._tmp)
        self.assertEqual(resolved, os.path.join(os.path.realpath(self._tmp), 'tapError.json'))

    def test_legitimate_nested_relative_path_is_allowed(self):
        resolved = validate_error_file_path('123/import-file-copy-execution/tapError.json', base_dir=self._tmp)
        self.assertTrue(resolved.startswith(os.path.realpath(self._tmp) + os.sep))

    def test_relative_traversal_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            validate_error_file_path('../../etc/passwd', base_dir=self._tmp)

    def test_absolute_path_outside_base_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            validate_error_file_path('/etc/passwd', base_dir=self._tmp)

    def test_empty_path_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            validate_error_file_path('', base_dir=self._tmp)

    def test_none_path_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            validate_error_file_path(None, base_dir=self._tmp)


if __name__ == '__main__':
    unittest.main()
