"""File storage for generated certificates.

Only the local filesystem is implemented. Callers only use ``save``, ``path_for``
and ``exists``, so an S3/GCS implementation could be swapped in without touching
the API or the worker.
"""

import os
import tempfile
from pathlib import Path


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def save(self, job_id: str, certificate_id: str, content: bytes, extension: str) -> str:
        """Write a certificate and return its storage key (path relative to root).

        The path is derived from the IDs, so regenerating a certificate (e.g. on
        retry or crash recovery) overwrites the same file instead of creating
        a duplicate.
        """
        key = f"{job_id}/{certificate_id}.{extension}"
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write to a temp file then rename: os.replace is atomic, so a crash
        # mid-write never leaves a half-written PDF at the final path.
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            os.replace(tmp_path, target)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
        return key

    def path_for(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()
