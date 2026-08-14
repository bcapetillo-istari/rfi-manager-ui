"""T6 (persistence parts) — project-file round-trip, atomic writes surviving a
simulated crash mid-write, and the LLM scratch cache."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from rfi_manager.models import PipelineState, Project, ResponseRecord
from rfi_manager.persistence import (
    cache_llm_output,
    clear_cached_llm_output,
    llm_cache_dir,
    load_cached_llm_output,
    load_project,
    save_project,
)


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


# ---------------------------------------------------------------- LLM cache

def test_llm_cache_round_trip(tmp_path: Path):
    project_path = tmp_path / "demo.rfiproj"
    cache_path = cache_llm_output(project_path, "resp-9", "RAW LLM OUTPUT")
    assert cache_path.is_relative_to(llm_cache_dir(project_path))
    assert load_cached_llm_output(cache_path) == "RAW LLM OUTPUT"


def test_llm_cache_missing_returns_none(tmp_path: Path):
    assert load_cached_llm_output(tmp_path / "nope.txt") is None


def test_clear_llm_cache(tmp_path: Path):
    project_path = tmp_path / "demo.rfiproj"
    cache_path = cache_llm_output(project_path, "resp-9", "RAW")
    clear_cached_llm_output(cache_path)
    assert load_cached_llm_output(cache_path) is None
    clear_cached_llm_output(None)  # no-op, no error
