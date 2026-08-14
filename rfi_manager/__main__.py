"""Entry point: ``python -m rfi_manager``.

Loads config.toml + env secrets, builds the real adapters, and launches the
main window. (``--selftest`` with fake adapters lands in M4.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rfi_manager", description="RFI Manager")
    parser.add_argument("--config", default="config.toml", help="path to config.toml")
    parser.add_argument(
        "--project-dir", default=None,
        help="directory for .rfiproj files (default: ask on first save)",
    )
    args = parser.parse_args(argv)

    from .config import ConfigError, load_config

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    from .istari_adapter import IstariAdapter

    istari = IstariAdapter(config.istari)

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication(sys.argv[:1])
    window = MainWindow(
        istari,
        llm_provider=config.llm.provider,
        llm_model=config.llm.model,
        project_dir=Path(args.project_dir) if args.project_dir else None,
        poll_interval_s=config.istari.job_poll_interval_s,
        job_timeout_s=config.istari.job_timeout_s,
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
