"""``uv run filmscan-develop RAW [RAW...]`` — batch develop from the command line.

The brief demands strict module separation, and this is the Developer half:
it never imports the camera layer. It takes raw files plus calibration plus a
JSON params file and writes 16-bit TIFFs (+ optional JPEG proofs) with metadata
sidecars. Reproducibility is the point, hence params-from-file rather than a
pile of flags that cannot be diffed back into a project.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from filmscan_studio.core.exposure import ExposureSettings
from filmscan_studio.core.filmic import FilmicProfile
from filmscan_studio.core.positive import PositiveParams
from filmscan_studio.developer.pipeline import (
    DeveloperParams,
    DeveloperPipeline,
    sidecar_for,
    write_jpeg,
    write_tiff,
)

log = logging.getLogger(__name__)


def _params_from_file(path: Path | None) -> DeveloperParams:
    """Load develop parameters, or defaults.

    Schema mirrors :meth:`DeveloperParams.fingerprint` input so a params file can
    be round-tripped from a sidecar of a previous export.
    """
    if path is None:
        return DeveloperParams()
    data = json.loads(path.read_text(encoding="utf-8"))
    profile = FilmicProfile(**data.get("profile", {}))
    positive = PositiveParams(
        exposure_ev=data.get("exposure_ev", 0.0),
        base_level=data.get("base_level"),
        profile=profile,
        invert=data.get("invert", True),
    )
    return DeveloperParams(
        positive=positive, half_size=data.get("half_size", False)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="filmscan-develop")
    parser.add_argument("raws", nargs="+", type=Path, help="RAW soubory (NEF/DNG)")
    parser.add_argument("-o", "--output", type=Path, default=Path("."))
    parser.add_argument("--dark", type=Path, help="složka nebo soubory dark frameů")
    parser.add_argument("--flat", type=Path, help="složka nebo soubory flat fieldu")
    parser.add_argument(
        "--dark-exposure", type=float,
        help="času dark frameů v sekundách (přepíše EXIF prvního souboru)",
    )
    parser.add_argument(
        "--flat-exposure", type=float,
        help="času flatu v sekundách (přepíše EXIF prvního souboru)",
    )
    parser.add_argument("--params", type=Path, help="JSON s parametry vyvolání")
    parser.add_argument("--jpeg", action="store_true", help="zapsat i JPEG kontrolu")
    parser.add_argument("--colour", action="store_true", help="barevný průchod (demosaic)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    params = _params_from_file(args.params)
    dark_paths = _expand(args.dark)
    flat_paths = _expand(args.flat)
    # Calibration exposure comes from EXIF when present -- the whole point of the
    # brief's "flat need not match scan exposure" rule is that it is *recorded*,
    # not guessed. Flags override for files with stripped metadata.
    dark_exposure = _exposure_of(dark_paths, args.dark_exposure)
    flat_exposure = _exposure_of(flat_paths, args.flat_exposure)
    pipeline = DeveloperPipeline.from_files(
        dark_paths, flat_paths,
        dark_exposure=dark_exposure, flat_exposure=flat_exposure,
    )
    args.output.mkdir(parents=True, exist_ok=True)

    failures = 0
    for raw in args.raws:
        try:
            if args.colour:
                result = pipeline.develop_colour(raw, params)
            else:
                from filmscan_studio.core.rawio import open_frame

                result = pipeline.develop(open_frame(raw), params)
            tiff = write_tiff(result, args.output / f"{raw.stem}.tif")
            written = [str(tiff)]
            if args.jpeg:
                written.append(str(write_jpeg(result, args.output / f"{raw.stem}.jpg")))
            sidecar_path = args.output / f"{raw.stem}.develop.json"
            sidecar_path.write_text(
                json.dumps(sidecar_for(result), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            written.append(str(sidecar_path))
            print(f"{raw.name} -> {', '.join(written)} [{result.fingerprint[:12]}]")
        except Exception as exc:  # noqa: BLE001 - batch must continue over failures
            failures += 1
            log.error("%s: %s", raw.name, exc)
    return 1 if failures else 0


def _exposure_of(paths: list[Path], override: float | None) -> ExposureSettings:
    """EXIF exposure of the first calibration file, or the override."""
    if override:
        return ExposureSettings(shutter=override)
    if not paths:
        # No calibration frames at all: the value is never consulted, and
        # from_files() ignores it. A placeholder keeps the signature honest.
        return ExposureSettings(shutter=1.0)
    if paths:
        from filmscan_studio.core.rawio import read_exif

        exif = read_exif(paths[0])
        if exif and exif.exposure_time:
            return ExposureSettings(
                shutter=exif.exposure_time,
                iso=exif.iso or 100,
                aperture=exif.f_number,
            )
    raise SystemExit(
        "Kalibrační soubory nemají EXIF čas -- zadej --dark-exposure / --flat-exposure."
    )


def _expand(path: Path | None) -> list[Path]:
    if path is None:
        return []
    if path.is_dir():
        return sorted(
            p
            for p in path.iterdir()
            if p.suffix.lower() in {".nef", ".dng", ".npy", ".tif", ".tiff"}
        )
    return [path]


if __name__ == "__main__":
    raise SystemExit(main())
