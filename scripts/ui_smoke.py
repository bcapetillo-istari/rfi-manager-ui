"""Headless end-to-end UI smoke: exercises every MVP flow against fakes.

Run:  QT_QPA_PLATFORM=offscreen python scripts/ui_smoke.py
Exits nonzero on failure. No network, no credentials.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from rfi_manager.models import PipelineState
from rfi_manager.persistence import load_project, save_project
from rfi_manager.ui.main_window import MainWindow
from tests.fakes import FakeIstari

REQS = json.dumps([
    {"id": "C-01", "label": "MOSA", "description": "MOSA compliance", "type": "enum",
     "unit": None, "options": ["Compliant", "Partial"], "required": True},
    {"id": "C-02", "label": "Weight (kg)", "description": "Unit weight", "type": "numeric",
     "unit": "kg", "options": None, "required": False},
])


def ans(a, b, conf="medium"):
    return json.dumps([
        {"id": "C-01", "value": a, "unit": None, "quote": "q1", "page": 1,
         "confidence": "high"},
        {"id": "C-02", "value": b, "unit": "kg", "quote": "q2", "page": 2,
         "confidence": conf},
    ])


def pump(app, cond, what, timeout=15):
    deadline = time.time() + timeout
    while not cond():
        app.processEvents()
        assert time.time() < deadline, f"TIMEOUT: {what}"


def main() -> None:
    app = QApplication([])
    istari = FakeIstari()
    istari.add_credential("pat", auth_type="istari")
    istari.add_credential("key", auth_type="llm")
    rfi = istari.add_model("rfi.pdf", text="RFI TEXT")
    ra = istari.add_model("acme.pdf", text="ACME TEXT")
    rb = istari.add_model("beta.pdf", text="BETA TEXT")
    tmp = Path(tempfile.mkdtemp())

    # ---- 0. Connection bar: start disconnected, guard fires, then connect
    win = MainWindow(adapter_factory=lambda config: istari,
                     llm_provider="claude", llm_model="claude-opus-5",
                     project_dir=tmp, poll_interval_s=0)
    win.show()
    assert "not connected" in win.connection_label.text()
    with mock.patch.object(QMessageBox, "warning") as w:
        win.start_extraction("anything", "")
        assert w.called, "actions must be blocked before connecting"
    win.registry_url_edit.setText("https://fake.istari.example")
    win.pat_edit.setText("fake-pat")
    win.connect_to_registry()
    pump(app, lambda: "connected as" in win.connection_label.text(), "connect")
    pump(app, lambda: win.llm_cred_combo.count() == 2, "credentials")
    print("0. connection bar (guard, connect, credentials load): OK")

    # ---- 1. Stage 1 + review edits + commit (FR1/FR2)
    istari.queue_llm_output(REQS)
    win.start_extraction(rfi.model_id, "")
    pump(app, lambda: win._stack.currentWidget() is win.review_screen, "review screen")
    win.review_screen._add_row()
    app.processEvents()
    assert not win.review_screen.commit_button.isEnabled(), "commit enabled on invalid row"
    win.review_screen.table.setCurrentCell(2, 0)
    win.review_screen._delete_row()
    app.processEvents()
    assert win.review_screen.commit_button.isEnabled(), "commit not re-enabled"
    win.review_screen._on_commit()
    pump(app, lambda: win.project and win.project.schema_version == "1.0", "commit v1.0")
    print("1. stage1 + review edit + commit: OK")

    # ---- 2. Mixed batch: one good, one failing validation twice (FR4, §4 retry-once)
    istari.queue_llm_output(ans("Compliant", 38.5))
    istari.queue_llm_output("garbage")
    istari.queue_llm_output("garbage again")
    win.start_ingest([ra.latest_revision_id, rb.latest_revision_id], False)
    pump(app, lambda: win.project.response_for(rb.model_id)
         and win.project.response_for(rb.model_id).state is PipelineState.FAILED
         and win.project.response_for(ra.model_id).state is PipelineState.DONE,
         "mixed batch")
    assert win.project.response_for(rb.model_id).llm_attempts == 2, "retry-once counter"
    pump(app, lambda: win.comparison_page.model.rowCount() == 1, "table 1 row")
    pump(app, lambda: any(
        win.stage2_page.status_table.item(r, 0).text() == rb.model_id
        and win.stage2_page.status_table.item(r, 1).text() == "failed"
        and "failed validation" in win.stage2_page.status_table.item(r, 2).text()
        for r in range(win.stage2_page.status_table.rowCount())), "failed row shown")
    print("2. mixed batch (1 done, 1 failed after exactly one retry, reason shown): OK")

    # ---- 3. FR4 retry via the UI
    istari.queue_llm_output(ans("Partial", 41.0, "low"))
    win.retry_response(rb.model_id)
    pump(app, lambda: win.project.response_for(rb.model_id).state is PipelineState.DONE,
         "retry done")
    pump(app, lambda: win.comparison_page.model.rowCount() == 2, "table 2 rows")
    print("3. UI retry of failed response: OK")

    # ---- 4. FR11 resume: crash mid-LLM-job, dead job id -> clean restart
    proj_path = tmp / f"{rfi.model_id}.rfiproj"
    proj = load_project(proj_path)
    rec = proj.response_for(rb.model_id)
    rec.state = PipelineState.LLM_JOB_SUBMITTED
    rec.llm_job_id = "llmjob-vanished"
    rec.llm_attempts = 1
    save_project(proj, proj_path)

    win2 = MainWindow(istari, project_dir=tmp, poll_interval_s=0)
    win2.show()
    pump(app, lambda: win2.llm_cred_combo.count() == 2, "win2 creds")
    with mock.patch.object(QMessageBox, "question",
                           return_value=QMessageBox.StandardButton.Yes) as q:
        win2.open_project(proj_path)
        pump(app, lambda: q.called, "resume prompt shown")
        pump(app, lambda: win2.project.response_for(rb.model_id).state
             is PipelineState.DONE, "resumed to done")
    assert "Resume 1 incomplete" in q.call_args[0][2], q.call_args
    pump(app, lambda: "restarting from queued" in win2.log_view.toPlainText(),
         "FR11 note")
    pump(app, lambda: win2.comparison_page.model.rowCount() == 2, "win2 table")
    print("4. FR11 resume prompt + dead-LLM-job clean restart: OK")

    # ---- 5. FR3 re-run: warning, bumped schema, stale flags
    istari.queue_llm_output(REQS)
    with mock.patch.object(QMessageBox, "question",
                           return_value=QMessageBox.StandardButton.Yes) as q:
        win2.start_extraction(rfi.model_id, "")
        pump(app, lambda: win2._stack.currentWidget() is win2.review_screen,
             "review v1.1")
        assert q.called, "FR3 warning not shown"
        assert win2.review_screen.schema_edit.text() == "1.1", (
            "schema not bumped: " + win2.review_screen.schema_edit.text())
        win2.review_screen._on_commit()
        pump(app, lambda: win2.project.schema_version == "1.1", "commit v1.1")
    win2.refresh_comparison()
    # wait for the post-commit refresh: rows re-fetched and flagged stale
    pump(app, lambda: win2.comparison_page.model.rowCount() == 2
         and all(win2.comparison_page.model.row_at(i).stale for i in range(2)),
         "old answers flagged stale after v1.1 commit")
    win2.comparison_page.proxy.set_filter_mode("stale schema")
    assert win2.comparison_page.proxy.rowCount() == 2
    win2.comparison_page.proxy.set_filter_mode("has NOT_FOUND")
    assert win2.comparison_page.proxy.rowCount() == 0
    win2.comparison_page.proxy.set_filter_mode("all")
    print("5. FR3 re-run warning + schema bump + stale flags/filter: OK")

    # ---- 6. Re-ingest under v1.1; FR5 idempotent skip; FR12 rebuild
    istari.queue_llm_output(ans("Compliant", 38.5))
    istari.queue_llm_output(ans("Partial", 41.0, "low"))
    win2.start_ingest([ra.latest_revision_id, rb.latest_revision_id], False)
    pump(app, lambda: all(r.state is PipelineState.DONE and r.schema_version == "1.1"
                          for r in win2.project.responses), "re-ingest v1.1")
    llm_calls_before = len(istari.llm_calls)
    win2.start_ingest([ra.latest_revision_id], False)
    pump(app, lambda: win2.project.response_for(ra.model_id).state
         is PipelineState.DONE, "skip")
    app.processEvents(); time.sleep(0.1); app.processEvents()
    assert len(istari.llm_calls) == llm_calls_before, "FR5 skip submitted an LLM job"

    # force re-extract on a DONE record must actually re-run (FR5 bypass)
    istari.queue_llm_output(ans("Compliant", 39.0))
    win2.start_ingest([ra.latest_revision_id], True)
    pump(app, lambda: len(istari.llm_calls) == llm_calls_before + 1
         and win2.project.response_for(ra.model_id).state is PipelineState.DONE,
         "force re-extract re-ran")

    win3 = MainWindow(istari, project_dir=Path(tempfile.mkdtemp()), poll_interval_s=0)
    win3.show()
    win3.open_from_rfi(rfi.model_id)
    pump(app, lambda: win3.project is not None
         and win3.comparison_page.model.rowCount() == 2, "rebuild")
    assert win3.project.schema_version == "1.1", "rebuild should pick highest schema"
    print("6. FR5 idempotent re-ingest + FR12 rebuild picks v1.1: OK")

    # ---- 7. FR7 detail pane, numeric sort, search
    win3.comparison_page.table.selectRow(0)
    app.processEvents()
    detail = win3.comparison_page.detail.toPlainText()
    assert "Answers artifact UUID: artifact-" in detail
    assert "Schema version: 1.1" in detail
    proxy = win3.comparison_page.proxy
    proxy.sort(2, Qt.SortOrder.AscendingOrder)
    vals = [proxy.index(r, 2).data() for r in range(proxy.rowCount())]
    assert vals == ["39.0", "41.0"], vals  # 39.0 written by the force re-extract
    win3.comparison_page.search_edit.setText("acme")
    assert proxy.rowCount() == 1
    win3.comparison_page.search_edit.setText("")
    print("7. FR7 detail pane + numeric sort + search: OK")

    # ---- 8. Missing-credentials guard
    istari2 = FakeIstari()
    istari2.add_model("rfi2.pdf", text="X")
    win4 = MainWindow(istari2, project_dir=Path(tempfile.mkdtemp()), poll_interval_s=0)
    win4.show()
    with mock.patch.object(QMessageBox, "warning") as w:
        win4.start_extraction("model-1", "")
        app.processEvents()
        assert w.called, "should warn when no credentials selected"
    print("8. missing-credentials guard: OK")

    # ---- 9. FR8 Commit/Observe in Istari from the comparison page
    # (T/O validation adds validation_report.json/.html — 5 artifacts total)
    uploads_before = len(istari.upload_calls)
    win3.comparison_page.commit_button.click()
    pump(app, lambda: len(istari.upload_calls) == uploads_before + 5, "commit uploads")
    committed = istari.upload_calls[uploads_before:]
    names = {c["name"] for c in committed}
    assert names == {"answers.csv", "answers_tidy.json", "review.html",
                     "validation_report.json", "validation_report.html"}, names
    assert all(c["model_id"] == win3.project.rfi_uuid for c in committed)
    tidy_payload = next(c["payload"] for c in committed if c["name"] == "answers_tidy.json")
    assert len(tidy_payload["rows"]) == 4  # 2 responses x 2 requirements
    validation_payload = next(
        c["payload"] for c in committed if c["name"] == "validation_report.json"
    )
    assert len(validation_payload["rows"]) == 4
    # REQS in this script carry no T/O fields -> everything NOT_GRADEABLE/NOT_FOUND
    assert all(
        r["grade"] in ("NOT_GRADEABLE", "NOT_FOUND") for r in validation_payload["rows"]
    )
    print("9. FR8 commit uploads 5 artifacts incl. validation reports: OK")

    # ---- 10. Review screen round-trips T/O fields (no silent data loss)
    from rfi_manager.models import Requirement
    from rfi_manager.ui.review_screen import ReviewScreen

    screen = ReviewScreen()
    to_reqs = [
        Requirement(id="1.1", label="Range", description="Range req", type="numeric",
                    unit="km", threshold=200, objective=1500, direction="at_least",
                    gradeable=True, to_raw="T=200km/O=1500km"),
        Requirement(id="C-01", label="MOSA", description="d", type="enum",
                    options=["Non-Compliant", "Partial", "Compliant"],
                    threshold_option="Partial", objective_option="Compliant",
                    gradeable=True),
        Requirement(id="KSA-1", label="IFF", description="d", type="boolean",
                    threshold=True, objective=True, gradeable=True),
    ]
    screen.load(to_reqs, "1.1")
    assert screen.requirements() == to_reqs, "review screen dropped T/O fields"
    errors, _warnings = screen.validate()
    assert not errors, errors
    print("10. review screen T/O round-trip without data loss: OK")

    print("ALL EXTENDED UI CHECKS PASSED")


if __name__ == "__main__":
    main()
