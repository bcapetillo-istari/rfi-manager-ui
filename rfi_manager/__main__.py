"""Entry point: ``python -m rfi_manager``.

Connection to the Istari registry happens in the UI (Registry URL + PAT boxes,
PRD §3.3). ``config.toml`` is optional and only provides defaults: registry
URL prefill, LLM provider/model job parameters, timeouts. The ISTARI_TOKEN
env var, when set, prefills the PAT box. (``--selftest`` lands in M4.)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rfi_manager", description="RFI Manager")
    parser.add_argument("--config", default="config.toml", help="path to config.toml (optional)")
    parser.add_argument(
        "--project-dir", default=None,
        help="directory for .rfiproj files (default: ask on first save)",
    )
    args = parser.parse_args(argv)

    from .config import AppConfig, ConfigError, IstariConfig, LLMConfig, load_config

    try:
        config = load_config(args.config, require_token=False)
    except ConfigError as e:
        # no config file is fine — the UI collects the connection details
        print(f"note: {e} — starting with built-in defaults", file=sys.stderr)
        config = AppConfig(istari=IstariConfig(base_url="", token=""), llm=LLMConfig())

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    window = MainWindow(
        registry_url_prefill=config.istari.base_url,
        pat_prefill=os.environ.get("ISTARI_TOKEN", ""),
        llm_provider=config.llm.provider,
        llm_model=config.llm.model,
        project_dir=Path(args.project_dir) if args.project_dir else None,
        poll_interval_s=config.istari.job_poll_interval_s,
        job_timeout_s=config.istari.job_timeout_s,
        request_timeout_s=config.istari.request_timeout_s,
        retries=config.istari.retries,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
