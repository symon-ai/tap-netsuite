"""Regression tests for WP-32503 (CWE-73 path traversal in error_file_path).

These tests exercise ``tap_netsuite.resolve_safe_error_file_path`` in isolation.
The tap's ``__init__`` imports ``singer`` and the netsuite SOAP client at module
load time; those are network/SDK dependencies that are not needed to validate the
path-sanitization logic, so we install lightweight stub modules before importing
the package. No network, filesystem-outside-tmp, or NetSuite access occurs.
"""
import os
import sys
import types
import unittest


def _install_stub(name):
    mod = types.ModuleType(name)
    sys.modules.setdefault(name, mod)
    return mod


# --- Stub the heavy import chain so we can import tap_netsuite.__init__ ------
_requests = _install_stub('requests')
_requests.exceptions = _install_stub('requests.exceptions')
_requests.exceptions.ConnectionError = type('ConnectionError', (Exception,), {})

_singer = _install_stub('singer')


class _StubLogger:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def critical(self, *a, **k):
        pass


_singer.get_logger = lambda: _StubLogger()
_singer.utils = _install_stub('singer.utils')
_singer.metadata = _install_stub('singer.metadata')
_singer.metrics = _install_stub('singer.metrics')
_singer.ActivateVersionMessage = object

_netsuite = _install_stub('tap_netsuite.netsuite')
_netsuite.field_to_property_schema = lambda field: {}
_netsuite.NetSuite = object
_exceptions = _install_stub('tap_netsuite.netsuite.exceptions')
for _name in ('TapNetSuiteException', 'TapNetSuiteQuotaExceededException',
              'SymonException'):
    setattr(_exceptions, _name, type(_name, (Exception,), {}))
_sync = _install_stub('tap_netsuite.sync')
_sync.sync_stream = lambda *a, **k: None
_sync.get_stream_version = lambda *a, **k: None

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tap_netsuite import resolve_safe_error_file_path  # noqa: E402


class ResolveSafeErrorFilePathTests(unittest.TestCase):
    def setUp(self):
        # Use a controlled base directory so tests are hermetic.
        self.base = os.path.realpath(
            os.path.join(os.path.dirname(__file__), 'tmp_out'))
        os.makedirs(self.base, exist_ok=True)

    def test_legitimate_relative_path_is_allowed(self):
        resolved = resolve_safe_error_file_path('error.json', base_dir=self.base)
        self.assertEqual(resolved, os.path.join(self.base, 'error.json'))

    def test_legitimate_nested_relative_path_is_allowed(self):
        resolved = resolve_safe_error_file_path(
            'sub/error.json', base_dir=self.base)
        self.assertEqual(
            resolved, os.path.join(self.base, 'sub', 'error.json'))

    def test_parent_traversal_is_rejected(self):
        self.assertIsNone(
            resolve_safe_error_file_path(
                '../../etc/passwd', base_dir=self.base))

    def test_absolute_path_outside_base_is_rejected(self):
        self.assertIsNone(
            resolve_safe_error_file_path('/etc/passwd', base_dir=self.base))

    def test_empty_or_none_returns_none(self):
        self.assertIsNone(resolve_safe_error_file_path(None, base_dir=self.base))
        self.assertIsNone(resolve_safe_error_file_path('', base_dir=self.base))


if __name__ == '__main__':
    unittest.main()
