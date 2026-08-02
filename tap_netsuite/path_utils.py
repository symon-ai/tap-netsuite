"""Centralized path-validation helpers for tap-netsuite.

These routines exist to remediate CWE-73 (external control of file name or
path). Config-derived file paths (for example ``error_file_path`` supplied via
the tap config JSON) must never be passed to filesystem sinks without first
being constrained to an expected base directory, otherwise a tainted value such
as ``../../etc/passwd`` or an absolute path outside the working directory could
be used to read or overwrite arbitrary files.
"""

import os


class UnsafePathError(ValueError):
    """Raised when a config-derived path escapes its allowed base directory."""


def validate_error_file_path(error_file_path, base_dir=None):
    """Validate and normalize a config-derived error-file path.

    The ELT import-activity supplies ``error_file_path`` as a path underneath
    the per-import local working directory (a relative path resolved against the
    tap process's current working directory). This routine resolves the
    candidate path against ``base_dir`` (defaulting to the current working
    directory), fully normalizes it (collapsing any ``..`` segments and
    following symlinks), and guarantees the result stays inside ``base_dir``.

    Args:
        error_file_path: The raw, potentially tainted path from the tap config.
        base_dir: The directory the path must resolve inside of. Defaults to the
            current working directory, which is where the consumer places the
            per-import working directory.

    Returns:
        The fully-resolved absolute path, safe to hand to ``open()``.

    Raises:
        UnsafePathError: If ``error_file_path`` is empty/None or resolves to a
            location outside ``base_dir``.
    """
    if not error_file_path or not isinstance(error_file_path, str):
        raise UnsafePathError('error_file_path must be a non-empty string')

    if base_dir is None:
        base_dir = os.getcwd()

    base_real = os.path.realpath(base_dir)
    # os.path.join discards base_real when error_file_path is absolute, so an
    # absolute path outside the base is still caught by the containment check.
    candidate = os.path.realpath(os.path.join(base_real, error_file_path))

    if candidate != base_real and not candidate.startswith(base_real + os.sep):
        raise UnsafePathError(
            'error_file_path resolves outside the allowed base directory'
        )

    return candidate
