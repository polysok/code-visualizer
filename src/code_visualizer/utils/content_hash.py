"""Content fingerprint of a source file, stored on its ``File`` node.

Same formula as git's blob id (``git hash-object``): ``sha1("blob <size>\\0" +
bytes)``. It tells, without parsing anything, which files differ from what the
graph holds, whatever happened on disk (edits, branch switches, ``git
restore``, uncommitted changes) and whether the folder is a git repository or
not.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional, Union


def content_hash(data: bytes) -> str:
    digest = hashlib.sha1(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def file_content_hash(path: Union[str, Path]) -> Optional[str]:
    """Fingerprint of the file at *path*, None when it cannot be read."""
    try:
        return content_hash(Path(path).read_bytes())
    except OSError:
        return None
