"""Project-file persistence (PRD §3.6a).

The ``.rfiproj`` file is a pointer cache only — the platform is the source of
truth. It is written at EVERY state transition, atomically: write a temp file
in the same directory, fsync, then rename over the original. (The post-LLM
checkpoint is the LLM job's raw-output artifact on the platform — there is no
local LLM cache; see PRD §3.6b.)
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from .models import Project

# Parallel response processing (pipeline.process_responses) checkpoints from
# multiple threads; serializing saves keeps each written snapshot internally
# consistent. The write itself was already atomic (temp + fsync + rename).
_SAVE_LOCK = threading.Lock()


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file, fsync, rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_project(project: Project, path: str | Path) -> None:
    """Persist the project index atomically (called on every state
    transition). Thread-safe: serialization AND write happen under one lock
    so concurrent response threads each persist a coherent snapshot."""
    with _SAVE_LOCK:
        _atomic_write_text(Path(path), json.dumps(project.to_dict(), indent=2))


def load_project(path: str | Path) -> Project:
    """Load a ``.rfiproj`` file; raises ValueError on unsupported format."""
    with Path(path).open(encoding="utf-8") as f:
        return Project.from_dict(json.load(f))
