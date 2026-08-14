"""T6 (persistence parts) — project-file round-trip and atomic writes
surviving a simulated crash mid-write. (The post-LLM checkpoint is a platform
artifact, not a local cache — see test_resume_rebuild.py.)"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from rfi_manager.models import PipelineState, Project, ResponseRecord
from rfi_manager.persistence import load_project, save_project


def make_project() -> Project:
    return Project(
        rfi_uuid="rfi-123",
        rfi_revision="rev-1",
        requirements_artifact_uuid="art-55",
        schema_version="1.0",
        responses=[ResponseRecord(uuid="resp-9", state=PipelineState.JOB_SUBMITTED,
                                  job_id="job-7")],
    )


def test_save_load_round_trip(tmp_path: Path):
    path = tmp_path / "demo.rfiproj"
    project = make_project()
    save_project(project, path)
    assert load_project(path) == project


def test_saved_file_is_json_with_version(tmp_path: Path):
    path = tmp_path / "demo.rfiproj"
    save_project(make_project(), path)
    data = json.loads(path.read_text())
    assert data["format_version"] == Project.FORMAT_VERSION


def test_atomic_write_leaves_no_temp_files(tmp_path: Path):
    path = tmp_path / "demo.rfiproj"
    save_project(make_project(), path)
    save_project(make_project(), path)  # overwrite path too
    assert [p.name for p in tmp_path.iterdir()] == ["demo.rfiproj"]


def test_crash_mid_write_preserves_original(tmp_path: Path):
    """Simulated crash during the write: original file intact, temp file
    present-or-cleaned, and the original still loads (PRD §3.6a, T6)."""
    path = tmp_path / "demo.rfiproj"
    original = make_project()
    save_project(original, path)

    changed = make_project()
    changed.responses[0].transition(PipelineState.FAILED, error="boom")

    with mock.patch("rfi_manager.persistence.os.replace",
                    side_effect=OSError("simulated crash at rename")):
        with pytest.raises(OSError, match="simulated crash"):
            save_project(changed, path)

    # Original untouched and loadable
    assert load_project(path) == original


def test_crash_before_fsync_preserves_original(tmp_path: Path):
    path = tmp_path / "demo.rfiproj"
    original = make_project()
    save_project(original, path)

    with mock.patch("rfi_manager.persistence.os.fsync",
                    side_effect=OSError("simulated crash at fsync")):
        with pytest.raises(OSError):
            save_project(make_project(), path)

    assert load_project(path) == original
    # failed temp file is cleaned up
    assert [p.name for p in tmp_path.iterdir()] == ["demo.rfiproj"]
