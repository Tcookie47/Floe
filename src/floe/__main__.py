"""Entry point: python -m floe."""

from __future__ import annotations

import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from floe.core import diagnostics
    from floe.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Floe")
    app.setOrganizationName("tcookie")

    diagnostics.setup_logging()

    window = MainWindow()
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
