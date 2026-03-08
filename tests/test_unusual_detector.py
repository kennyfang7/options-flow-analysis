from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------

def test_unusual_detector_settings_defaults():
    """All unusual detector settings load with correct defaults."""
    s = Settings()
    assert s.unusual_premium_threshold == 250_000.0
    assert s.unusual_oi_ratio_threshold == 0.50
    assert s.unusual_signal_threshold == 5.0
    assert s.otm_delta_threshold == 0.30
    assert s.otm_premium_threshold == 100_000.0


def test_unusual_premium_threshold_must_exceed_min_premium():
    """ValidationError when unusual_premium_threshold <= min_premium."""
    with pytest.raises(ValidationError, match="unusual_premium_threshold.*must exceed min_premium"):
        Settings(min_premium=100.0, unusual_premium_threshold=50.0)

    with pytest.raises(ValidationError, match="unusual_premium_threshold.*must exceed min_premium"):
        Settings(min_premium=100.0, unusual_premium_threshold=100.0)


def test_unusual_premium_threshold_valid_when_above_min_premium():
    """No error when unusual_premium_threshold > min_premium."""
    s = Settings(min_premium=100.0, unusual_premium_threshold=200.0)
    assert s.unusual_premium_threshold == 200.0


def test_oi_ratio_threshold_must_be_positive():
    """ValidationError when unusual_oi_ratio_threshold <= 0."""
    with pytest.raises(ValidationError, match="unusual_oi_ratio_threshold must be greater than 0"):
        Settings(unusual_oi_ratio_threshold=0.0)

    with pytest.raises(ValidationError, match="unusual_oi_ratio_threshold must be greater than 0"):
        Settings(unusual_oi_ratio_threshold=-1.0)


def test_otm_delta_threshold_must_be_between_0_and_1():
    """ValidationError when otm_delta_threshold is 0 or 1."""
    with pytest.raises(ValidationError, match="otm_delta_threshold must be between 0 and 1"):
        Settings(otm_delta_threshold=0.0)

    with pytest.raises(ValidationError, match="otm_delta_threshold must be between 0 and 1"):
        Settings(otm_delta_threshold=1.0)


def test_unusual_signal_threshold_must_be_positive():
    """ValidationError when unusual_signal_threshold <= 0."""
    with pytest.raises(ValidationError, match="unusual_signal_threshold must be greater than 0"):
        Settings(unusual_signal_threshold=0.0)
