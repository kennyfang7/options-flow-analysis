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
