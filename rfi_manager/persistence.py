"""Project-file persistence (PRD §3.6a) and the LLM scratch cache (§3.6b).

The ``.rfiproj`` file is a pointer cache only — the platform is the source of
truth. It is written at EVERY state transition, atomically: write a temp file
in the same directory, fsync, then rename over the original.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .models import Project


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
    """Persist the project index atomically (called on every state transition)."""
    _atomic_write_text(Path(path), json.dumps(project.to_dict(), indent=2))


def load_project(path: str | Path) -> Project:
    """Load a ``.rfiproj`` file; raises ValueError on unsupported format."""
    with Path(path).open(encoding="utf-8") as f:
        return Project.from_dict(json.load(f))


def llm_cache_dir(project_path: str | Path) -> Path:
    """Scratch directory for raw LLM outputs, next to the project file."""
    p = Path(project_path)
    return p.parent / ".llm_cache" / p.stem


def cache_llm_output(project_path: str | Path, response_uuid: str, raw: str) -> Path:
    """Checkpoint raw LLM output so a crash before upload never re-pays the
    LLM call (PRD §3.6b). Returns the cache file path to record in the
    ResponseRecord."""
    path = llm_cache_dir(project_path) / f"{response_uuid}.txt"
    _atomic_write_text(path, raw)
    return path


def load_cached_llm_output(cache_path: str | Path) -> str | None:
    """Read a checkpointed LLM output; None if the cache file is gone."""
    p = Path(cache_path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def clear_cached_llm_output(cache_path: str | Path | None) -> None:
    """Delete a scratch file once its answers artifact is safely uploaded."""
    if cache_path:
        Path(cache_path).unlink(missing_ok=True)
