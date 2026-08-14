import json
import os
from pathlib import Path


def write_error_info(error_file_path, error_info):
    """Write error details only within the tap's working directory."""
    working_directory = Path.cwd().resolve()
    configured_path = working_directory / error_file_path
    destination = configured_path.parent.resolve() / configured_path.name

    try:
        destination.parent.relative_to(working_directory)
    except ValueError as exc:
        raise ValueError("error_file_path must be within the working directory") from exc

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    file_descriptor = os.open(destination, flags, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as error_file:
        json.dump(error_info, error_file)
