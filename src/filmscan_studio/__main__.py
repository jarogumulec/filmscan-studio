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


def _run_sdk_probe(args: argparse.Namespace) -> int:
    """Probe the Nikon SDK — but only an x86_64 interpreter can load it.

    Re-execs under the Rosetta helper venv when invoked from the arm64 one,
    so ``uv run filmscan-studio sdk-probe`` works from either.
    """
    import os
    import platform
    from pathlib import Path

    probe_argv: list[str] = []
    if args.out:
        probe_argv += ["--out", str(args.out)]
    if args.probe_capture:
        probe_argv.append("--capture")
    if args.probe_mf:
        probe_argv.append("--mf-test")

    helper = None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".venv-x86" / "bin" / "python"
        if candidate.exists():
            helper = candidate
            break
    if platform.machine() != "x86_64":
        if helper is None:
            print("Nikon SDK je x86_64-only. Spusťte scripts/install_helper.sh.",
                  file=sys.stderr)
            return 2
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)   # arm64 site-packages must not leak
        os.execve(str(helper),
                  [str(helper), "-m", "filmscan_studio.capture.sdk_probe",
                   *probe_argv], env)  # never returns
    # x86_64 interpreter: run in-process when the package is importable
    # (scripts/probe.sh); otherwise re-exec the helper venv so an editable
    # install isn't required.
    try:
        from filmscan_studio.capture.sdk_probe import main as probe_main
    except ModuleNotFoundError:
        if helper is None:
            print("Nikon SDK je x86_64-only. Spusťte scripts/install_helper.sh.",
                  file=sys.stderr)
            return 2
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        os.execve(str(helper),
                  [str(helper), "-m", "filmscan_studio.capture.sdk_probe",
                   *probe_argv], env)
    return probe_main(probe_argv)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filmscan-studio")
    parser.add_argument(
        "module",
        nargs="?",
        default="capture",
        choices=("capture", "sdk-probe"),
        help="Který modul spustit (Developer zatím nemá GUI).",
    )
    parser.add_argument("--out", default=None, help="sdk-probe: výstupní složka")
    parser.add_argument("--probe-capture", action="store_true",
                        help="sdk-probe: otestovat i still + stažení NEF")
    parser.add_argument("--probe-mf", action="store_true",
                        help="sdk-probe: zkusit MfDrive (jen elektronický obj!)")
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
    if args.module == "sdk-probe":
        return _run_sdk_probe(args)
    return _run_capture(args)


if __name__ == "__main__":
    raise SystemExit(main())
