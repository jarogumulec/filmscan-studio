"""Film base / min point: region metering, exposure scaling, JSON history.

The measurement is the raw material of a per-film H-D characterisation, so the
two properties that must hold are: the rect means exactly the pixels it covers
(in the array's own grid — the binned-stream/full-frame collision the brief
warns about), and the exposure bookkeeping scales a 1 s base reading onto a
4 s frame without dragging the pedestal along.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

from filmscan_studio.core.filmbase import (
    FilmBaseSample,
    append_sample,
    load_samples,
    region_mean,
)


class TestRegionMean:
    def test_whole_array_without_rect(self):
        data = np.full((10, 20), 500.0)
        assert region_mean(data, None) == pytest.approx(500.0)

    def test_rect_means_only_the_rect(self):
        data = np.zeros((100, 100))
        data[10:50, 20:60] = 1000.0
        assert region_mean(data, (20, 10, 60, 50)) == pytest.approx(1000.0)

    def test_rect_clamps_to_frame(self):
        data = np.full((50, 50), 42.0)
        assert region_mean(data, (-10, -10, 1000, 1000)) == pytest.approx(42.0)

    def test_rect_outside_frame_raises(self):
        """Measuring nothing must not pass as a base level."""
        data = np.full((50, 50), 42.0)
        with pytest.raises(ValueError):
            region_mean(data, (100, 100, 200, 200))


class TestExposureScaling:
    def _sample(self, mean: float, shutter: float, gain: float | None = 1.0,
                black: float = 100.0) -> FilmBaseSample:
        return FilmBaseSample(kind="stream", mean_dn=mean, black_level=black,
                              white_level=65535.0, shutter=shutter, gain=gain)

    def test_shutter_ratio_scales_signal_not_pedestal(self):
        """Base at 1 s reads 5100 DN over a 100 DN pedestal; at 4 s the photon
        part (5000) quadruples to 20100 total — the pedestal never scales."""
        sample = self._sample(5100.0, 1.0)
        assert sample.scaled_above_black(4.0) == pytest.approx(20000.0)
        assert sample.scaled_mean(4.0) == pytest.approx(20100.0)

    def test_gain_ratio_folds_in_when_given(self):
        sample = self._sample(5100.0, 1.0, gain=1.0)
        # Twice the gain at the same shutter doubles the photon signal.
        assert sample.scaled_above_black(1.0, target_gain=2.0) == pytest.approx(10000.0)

    def test_shorter_exposure_scales_down(self):
        sample = self._sample(5100.0, 2.0)
        assert sample.scaled_mean(1.0) == pytest.approx(2600.0)

    def test_invalid_shutter_rejected(self):
        sample = self._sample(5100.0, 1.0)
        with pytest.raises(ValueError):
            sample.scaled_above_black(0.0)


class TestJsonHistory:
    def test_missing_file_is_empty_history(self, tmp_path: Path):
        assert load_samples(tmp_path / "film_base.json") == []

    def test_roundtrip_and_append_order(self, tmp_path: Path):
        path = tmp_path / "film_base.json"
        now = datetime.now().astimezone()
        first = FilmBaseSample(
            kind="stream", mean_dn=4211.5, black_level=0.0,
            white_level=65535.0, shutter=2.0, gain=1.0,
            sensor_temperature_c=-5.2, rect=(10, 20, 30, 40),
            source="stream", captured_at=now)
        append_sample(path, first)
        append_sample(path, FilmBaseSample(
            kind="frame", mean_dn=8400.0, black_level=0.0,
            white_level=65535.0, shutter=4.0, gain=1.0, source="base_001.tif"))
        stored = load_samples(path)
        assert [s.mean_dn for s in stored] == [4211.5, 8400.0]
        assert stored[0].rect == (10, 20, 30, 40)
        assert stored[0].captured_at == now
        assert stored[0].sensor_temperature_c == pytest.approx(-5.2)
        assert stored[1].kind == "frame"
        assert stored[1].gain == pytest.approx(1.0)
