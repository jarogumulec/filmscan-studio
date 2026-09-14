"""``uv run filmscan-studio`` / ``python -m filmscan_studio``.

The two modules are deliberately separate applications, per the brief's
requirement that acquisition and development never share a process lifetime:
a crashed develop must not drop the USB session with the camera mid-roll.
"""

from __future__ import annotations

import argparse
import logging
import sys


def _run_capture(args: argparse.Namespace) -> int:
    from PySide6.QtWidgets import QApplication

    from filmscan_studio.capture.mock import MockCamera
    from filmscan_studio.gui.capture_window import CaptureWindow

    app = QApplication(sys.argv)
    camera = MockCamera() if args.mock else None
    if args.mock:
        camera.connect()
    window = CaptureWindow(camera=camera)
    window.show()
    return app.exec()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filmscan-studio")
    parser.add_argument(
        "module",
        nargs="?",
        default="capture",
        choices=("capture",),
        help="Který modul spustit (Developer zatím nemá GUI).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Simulovaná kamera místo D750 (pro vývoj a testy).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return _run_capture(args)


if __name__ == "__main__":
    raise SystemExit(main())
