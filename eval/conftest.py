"""Eval harness (docs/EVAL_FRAMEWORK_PRD.md): fixtures, config stamping, and
the JSON results log.

Evals are TRUE-INTEGRATION runs — no mocks, no fakes (tests/fakes.py belongs
to the unit suite). Every eval calls the production code exactly as it ships
in rfi_manager, so a result always describes the CURRENT pipeline
configuration: extraction function/tool names, prompts, and libraries are
read from the code, never restated here.

Excluded from the plain `pytest` run (pytest.ini testpaths — the unit suite
must stay offline). Run explicitly:

    pytest eval -m "not live"    # offline subset (custom extraction only)
    pytest eval                  # full suite (needs ISTARI_BASE_URL/ISTARI_TOKEN)

Each run writes one JSON report to eval/results/, keyed by timestamp + git
SHA, stamped with the exact configuration it measured (§3.2).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from rfi_manager import istari_adapter as _adapter_mod  # noqa: E402
from rfi_manager.config import IstariConfig  # noqa: E402
from rfi_manager.istari_adapter import IstariAdapter  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "results"


# ------------------------------------------------------- selection flags
# First-class flags instead of -k string expressions. These only exist when
# `eval` is on the pytest command line (this conftest is loaded for it):
#
#   pytest eval --extraction custom              # offline path only
#   pytest eval --extraction istari              # live platform path only
#   pytest eval --req-id 1.1 --req-id 7.8        # only those requirements


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("eval", "RFI Manager eval framework")
    group.addoption(
        "--extraction",
        choices=("custom", "istari", "all"),
        default="all",
        help="which production extraction path to evaluate (default: all)",
    )
    group.addoption(
        "--req-id",
        action="append",
        default=None,
        metavar="ID",
        help="evaluate only these requirement ids (repeatable)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    mode = config.getoption("--extraction")
    req_ids = config.getoption("--req-id")
    if mode == "all" and not req_ids:
        return
    selected: list[pytest.Item] = []
    deselected: list[pytest.Item] = []
    for item in items:
        callspec = getattr(item, "callspec", None)
        params = callspec.params if callspec else {}
        keep = True
        if mode != "all":
            item_mode = params.get("extracted")
            if item_mode is not None and item_mode != mode:
                keep = False
            elif (
                item_mode is None
                and mode == "custom"
                and item.get_closest_marker("live")
            ):
                # unparametrized live tests (platform integrity) belong to
                # the istari path — an offline run drops them too
                keep = False
        if keep and req_ids:
            answer_id = params.get("answer_id")
            if answer_id is not None and answer_id not in req_ids:
                keep = False
        (selected if keep else deselected).append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


@pytest.fixture(scope="session")
def istari() -> IstariAdapter:
    """Live adapter from the same .env credentials the app uses. Live evals
    skip cleanly when credentials are absent (offline/CI runs)."""
    base_url = os.environ.get("ISTARI_BASE_URL", "")
    token = os.environ.get("ISTARI_TOKEN", "")
    if not base_url or not token:
        pytest.skip("live eval needs ISTARI_BASE_URL and ISTARI_TOKEN in .env")
    return IstariAdapter(IstariConfig(base_url=base_url, token=token))


# ------------------------------------------------- config stamping (§3.2)


def _git_sha() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "unknown"


def _config_stamp() -> dict:
    """Everything a reader needs to attribute a result to an exact
    configuration. The Istari extraction block is read FROM THE CODE —
    change istari_adapter.py and the stamp (and the behavior measured)
    changes with it."""
    return {
        "git_sha": _git_sha(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "libraries": {
            "pdfplumber": _pkg_version("pdfplumber"),
            "istari-digital-client": _pkg_version("istari-digital-client"),
        },
        "istari_extraction": {
            "function": _adapter_mod._EXTRACT_FUNCTION,
            "tool": _adapter_mod._EXTRACT_TOOL,
            "tool_version": _adapter_mod._EXTRACT_TOOL_VERSION,
            "operating_system": _adapter_mod._EXTRACT_OS,
            "text_artifact": _adapter_mod.EXTRACT_TEXT_ARTIFACT,
        },
        "registry": os.environ.get("ISTARI_BASE_URL", ""),
    }


# ------------------------------------------------- JSON results log (§3.2)

_results: list[dict] = []

_CATEGORY_LABEL = {
    "custom": "custom extraction",
    "istari": "istari (platform) extraction",
    "shared": "shared checks",
}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    """Stamp each report with its eval category (which extraction path the
    test exercised), so the summary and JSON log can group by it."""
    outcome = yield
    report = outcome.get_result()
    callspec = getattr(item, "callspec", None)
    mode = callspec.params.get("extracted") if callspec else None
    if mode is None and item.get_closest_marker("live"):
        mode = "istari"  # unparametrized live tests (platform integrity)
    report.eval_category = mode or "shared"


def _refined_outcome(report: pytest.TestReport) -> str:
    if hasattr(report, "wasxfail"):
        return "xfailed" if report.skipped else "xpassed"
    return report.outcome


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # record the call phase, plus setup-phase skips/errors (fixture failures)
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        _results.append(
            {
                "test": report.nodeid,
                "category": getattr(report, "eval_category", "shared"),
                "outcome": _refined_outcome(report),
                "duration_s": round(report.duration, 3),
                "detail": str(report.longrepr)[:2000] if not report.passed else None,
            }
        )


def _totals_by_category() -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for r in _results:
        bucket = totals.setdefault(r["category"], {})
        bucket[r["outcome"]] = bucket.get(r["outcome"], 0) + 1
    return totals


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    """Per-category pass/fail table (custom vs platform extraction) at the
    end of every eval run."""
    if not _results:
        return
    terminalreporter.write_sep("=", "eval results by category")
    for category, counts in sorted(_totals_by_category().items()):
        total = sum(counts.values())
        passed = counts.get("passed", 0) + counts.get("xfailed", 0)
        rest = ", ".join(
            f"{n} {name}" for name, n in sorted(counts.items())
            if name not in ("passed",)
        )
        line = f"{_CATEGORY_LABEL.get(category, category):<30} {passed}/{total} ok"
        if rest:
            line += f"   ({rest})"
        all_ok = passed == total
        terminalreporter.write_line(line, green=all_ok, red=not all_ok)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if not _results:
        return
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = _config_stamp()
    outcomes = [r["outcome"] for r in _results]
    payload = {
        "config": stamp,
        "exit_status": int(exitstatus),
        "totals": {o: outcomes.count(o) for o in sorted(set(outcomes))},
        "totals_by_category": _totals_by_category(),
        "results": _results,
    }
    name = (
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        f"-{stamp['git_sha']}.json"
    )
    path = RESULTS_DIR / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\neval results written: {path.relative_to(REPO_ROOT)}")
