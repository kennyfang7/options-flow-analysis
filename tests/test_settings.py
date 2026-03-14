from __future__ import annotations


def test_risk_free_rate_default():
    from config.settings import Settings
    s = Settings()
    assert s.risk_free_rate == 0.05


def test_risk_free_rate_override():
    from config.settings import Settings
    s = Settings(risk_free_rate=0.04)
    assert s.risk_free_rate == 0.04


def test_risk_free_rate_must_be_non_negative():
    import pytest
    from pydantic import ValidationError
    from config.settings import Settings
    with pytest.raises(ValidationError):
        Settings(risk_free_rate=-0.01)


def test_dashboard_settings_defaults():
    from config.settings import Settings
    s = Settings(
        min_premium=100.0,
        unusual_premium_threshold=200.0,
    )
    assert s.dashboard_refresh_fast == 5.0
    assert s.dashboard_refresh_slow == 10.0
    assert s.dashboard_max_rows == 50
    assert s.dashboard_max_alerts == 200


def test_dashboard_refresh_fast_must_be_positive():
    import pytest
    from config.settings import Settings
    with pytest.raises(Exception):
        Settings(
            min_premium=100.0,
            unusual_premium_threshold=200.0,
            dashboard_refresh_fast=0.0,
        )


def test_dashboard_max_rows_must_be_at_least_one():
    import pytest
    from config.settings import Settings
    with pytest.raises(Exception):
        Settings(
            min_premium=100.0,
            unusual_premium_threshold=200.0,
            dashboard_max_rows=0,
        )


def test_scanner_max_rows_default() -> None:
    from config.settings import Settings
    s = Settings()
    assert s.scanner_max_rows == 25


def test_scanner_max_rows_too_large_raises() -> None:
    import pytest
    from config.settings import Settings
    with pytest.raises(Exception):
        Settings(scanner_max_rows=51)


def test_scanner_location_default() -> None:
    from config.settings import Settings
    s = Settings()
    assert s.scanner_location == "STK.US.MAJOR"
