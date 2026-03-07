from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.settings import Settings


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------

def test_settings_flow_classifier_defaults():
    """New flow classifier fields load with correct defaults."""
    s = Settings()
    assert s.sweep_window_seconds == 2.0
    assert s.sweep_min_legs == 3
    assert s.split_window_seconds == 5.0
    assert s.split_min_legs == 3
    assert s.split_size_tolerance == 0.20
    assert s.classifier_window_seconds == 30.0
    assert s.aggressor_buy_threshold == 0.70
    assert s.aggressor_sell_threshold == 0.30


def test_settings_min_premium_must_be_positive():
    """Settings raises ValidationError when min_premium <= 0."""
    with pytest.raises(ValidationError, match="min_premium must be greater than 0"):
        Settings(min_premium=0.0)

    with pytest.raises(ValidationError, match="min_premium must be greater than 0"):
        Settings(min_premium=-1.0)


def test_settings_min_premium_positive_is_valid():
    """Settings accepts any positive min_premium."""
    s = Settings(min_premium=1.0)
    assert s.min_premium == 1.0


def test_settings_aggressor_thresholds_must_be_ordered():
    """Settings raises ValidationError when buy threshold <= sell threshold."""
    with pytest.raises(ValidationError, match="aggressor_buy_threshold must be greater than aggressor_sell_threshold"):
        Settings(aggressor_buy_threshold=0.30, aggressor_sell_threshold=0.70)
