from __future__ import annotations

"""Tests for src/utils/validators.py and the model-level validation it powers.

Covers:
- All pure helper functions (boundary values, None handling).
- OptionContract field validators (reject / clamp / coerce behaviour).
- TickUpdate field validators.
- _parse_ticker() gate behaviour (inverted spread, ValidationError fallback).
- _ticker_to_update() gate behaviour (no-price drop, ValidationError drop,
  inverted spread correction).
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from src.utils.validators import (
    clamp_delta,
    clamp_implied_vol,
    has_any_price,
    is_bid_ask_consistent,
    is_con_id_valid,
    is_delta_valid,
    is_expiry_valid,
    is_implied_vol_valid,
    is_price_valid,
    is_strike_valid,
    is_volume_valid,
    sanitize_right,
)


# ===========================================================================
# Pure helper functions
# ===========================================================================


class TestIsPriceValid:
    def test_none_is_valid(self):
        assert is_price_valid(None) is True

    def test_zero_is_valid(self):
        assert is_price_valid(0.0) is True

    def test_positive_is_valid(self):
        assert is_price_valid(5.25) is True

    def test_negative_is_invalid(self):
        assert is_price_valid(-0.01) is False

    def test_large_positive_is_valid(self):
        assert is_price_valid(9999.99) is True


class TestIsBidAskConsistent:
    def test_both_none_is_valid(self):
        assert is_bid_ask_consistent(None, None) is True

    def test_bid_none_is_valid(self):
        assert is_bid_ask_consistent(None, 5.0) is True

    def test_ask_none_is_valid(self):
        assert is_bid_ask_consistent(5.0, None) is True

    def test_bid_less_than_ask_is_valid(self):
        assert is_bid_ask_consistent(4.90, 5.10) is True

    def test_bid_equal_to_ask_is_valid(self):
        assert is_bid_ask_consistent(5.0, 5.0) is True

    def test_bid_greater_than_ask_is_invalid(self):
        assert is_bid_ask_consistent(5.10, 4.90) is False


class TestHasAnyPrice:
    def test_all_none_returns_false(self):
        assert has_any_price(None, None, None) is False

    def test_only_bid_returns_true(self):
        assert has_any_price(1.0, None, None) is True

    def test_only_ask_returns_true(self):
        assert has_any_price(None, 1.5, None) is True

    def test_only_last_returns_true(self):
        assert has_any_price(None, None, 1.2) is True

    def test_all_present_returns_true(self):
        assert has_any_price(1.0, 1.5, 1.2) is True


class TestIsStrikeValid:
    def test_positive_is_valid(self):
        assert is_strike_valid(500.0) is True

    def test_zero_is_invalid(self):
        assert is_strike_valid(0.0) is False

    def test_negative_is_invalid(self):
        assert is_strike_valid(-10.0) is False

    def test_small_positive_is_valid(self):
        assert is_strike_valid(0.5) is True


class TestIsExpiryValid:
    def test_valid_date_returns_true(self):
        assert is_expiry_valid("20260320") is True

    def test_empty_string_returns_false(self):
        assert is_expiry_valid("") is False

    def test_too_short_returns_false(self):
        assert is_expiry_valid("2026032") is False

    def test_non_digit_returns_false(self):
        assert is_expiry_valid("abcdefgh") is False

    def test_invalid_month_returns_false(self):
        assert is_expiry_valid("20261301") is False

    def test_invalid_day_returns_false(self):
        assert is_expiry_valid("20260132") is False

    def test_leap_day_valid_year_returns_true(self):
        assert is_expiry_valid("20240229") is True

    def test_leap_day_non_leap_year_returns_false(self):
        assert is_expiry_valid("20230229") is False


class TestIsConIdValid:
    def test_positive_is_valid(self):
        assert is_con_id_valid(12345) is True

    def test_none_is_valid(self):
        assert is_con_id_valid(None) is True

    def test_zero_is_invalid(self):
        assert is_con_id_valid(0) is False

    def test_negative_is_invalid(self):
        assert is_con_id_valid(-1) is False


class TestIsVolumeValid:
    def test_none_is_valid(self):
        assert is_volume_valid(None) is True

    def test_zero_is_valid(self):
        assert is_volume_valid(0) is True

    def test_positive_is_valid(self):
        assert is_volume_valid(500) is True

    def test_negative_is_invalid(self):
        assert is_volume_valid(-1) is False


class TestIsImpliedVolValid:
    def test_none_is_valid(self):
        assert is_implied_vol_valid(None) is True

    def test_typical_value_is_valid(self):
        assert is_implied_vol_valid(0.25) is True

    def test_zero_is_valid(self):
        assert is_implied_vol_valid(0.0) is True

    def test_at_upper_bound_is_valid(self):
        assert is_implied_vol_valid(50.0) is True

    def test_above_upper_bound_is_invalid(self):
        assert is_implied_vol_valid(50.01) is False

    def test_negative_is_invalid(self):
        assert is_implied_vol_valid(-0.01) is False


class TestIsDeltaValid:
    def test_none_is_valid(self):
        assert is_delta_valid(None) is True

    def test_mid_range_is_valid(self):
        assert is_delta_valid(0.5) is True

    def test_at_lower_bound_is_valid(self):
        assert is_delta_valid(-1.0) is True

    def test_at_upper_bound_is_valid(self):
        assert is_delta_valid(1.0) is True

    def test_above_upper_bound_is_invalid(self):
        assert is_delta_valid(1.01) is False

    def test_below_lower_bound_is_invalid(self):
        assert is_delta_valid(-1.01) is False


class TestClampImpliedVol:
    def test_none_returns_none(self):
        assert clamp_implied_vol(None) is None

    def test_valid_value_returned_unchanged(self):
        assert clamp_implied_vol(0.25) == 0.25

    def test_zero_returned_unchanged(self):
        assert clamp_implied_vol(0.0) == 0.0

    def test_at_upper_bound_returned_unchanged(self):
        assert clamp_implied_vol(50.0) == 50.0

    def test_above_upper_bound_returns_none(self):
        assert clamp_implied_vol(51.0) is None

    def test_negative_returns_none(self):
        assert clamp_implied_vol(-0.01) is None


class TestClampDelta:
    def test_none_returns_none(self):
        assert clamp_delta(None) is None

    def test_valid_value_returned_unchanged(self):
        assert clamp_delta(0.5) == 0.5

    def test_at_lower_bound_returned_unchanged(self):
        assert clamp_delta(-1.0) == -1.0

    def test_at_upper_bound_returned_unchanged(self):
        assert clamp_delta(1.0) == 1.0

    def test_above_upper_bound_returns_none(self):
        assert clamp_delta(1.5) is None

    def test_below_lower_bound_returns_none(self):
        assert clamp_delta(-1.5) is None


class TestSanitizeRight:
    def test_uppercase_c_is_accepted(self):
        assert sanitize_right("C") == "C"

    def test_lowercase_c_is_normalised(self):
        assert sanitize_right("c") == "C"

    def test_uppercase_p_is_accepted(self):
        assert sanitize_right("P") == "P"

    def test_lowercase_p_is_normalised(self):
        assert sanitize_right("p") == "P"

    def test_invalid_value_raises(self):
        with pytest.raises(ValueError):
            sanitize_right("X")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            sanitize_right("")


# ===========================================================================
# OptionContract model validation
# ===========================================================================


class TestOptionContractValidators:
    def _make(self, **kwargs):
        from src.data.chain_fetcher import OptionContract
        defaults = dict(symbol="SPY", expiry="20260320", strike=500.0, right="C")
        defaults.update(kwargs)
        return OptionContract(**defaults)

    def test_valid_contract_constructs(self):
        c = self._make()
        assert c.symbol == "SPY"

    def test_zero_strike_raises(self):
        with pytest.raises(ValidationError):
            self._make(strike=0.0)

    def test_negative_strike_raises(self):
        with pytest.raises(ValidationError):
            self._make(strike=-1.0)

    def test_empty_expiry_raises(self):
        with pytest.raises(ValidationError):
            self._make(expiry="")

    def test_invalid_expiry_raises(self):
        with pytest.raises(ValidationError):
            self._make(expiry="notadate")

    def test_invalid_right_raises(self):
        with pytest.raises(ValidationError):
            self._make(right="X")

    def test_lowercase_right_is_normalised(self):
        c = self._make(right="c")
        assert c.right == "C"

    def test_con_id_zero_coerced_to_none(self):
        c = self._make(con_id=0)
        assert c.con_id is None

    def test_con_id_none_stays_none(self):
        c = self._make(con_id=None)
        assert c.con_id is None

    def test_con_id_positive_unchanged(self):
        c = self._make(con_id=99999)
        assert c.con_id == 99999

    def test_out_of_range_iv_clamped_to_none(self):
        c = self._make(implied_vol=100.0)
        assert c.implied_vol is None

    def test_negative_iv_clamped_to_none(self):
        c = self._make(implied_vol=-0.1)
        assert c.implied_vol is None

    def test_valid_iv_unchanged(self):
        c = self._make(implied_vol=0.25)
        assert c.implied_vol == pytest.approx(0.25)

    def test_out_of_range_delta_clamped_to_none(self):
        c = self._make(delta=2.0)
        assert c.delta is None

    def test_valid_delta_unchanged(self):
        c = self._make(delta=0.45)
        assert c.delta == pytest.approx(0.45)

    def test_negative_volume_coerced_to_none(self):
        c = self._make(volume=-5)
        assert c.volume is None


# ===========================================================================
# TickUpdate model validation
# ===========================================================================


class TestTickUpdateValidators:
    def _make(self, **kwargs):
        from src.data.tick_stream import TickUpdate
        defaults = dict(
            symbol="SPY",
            con_id=12345,
            expiry="20260320",
            strike=500.0,
            right="C",
            timestamp=datetime.now(timezone.utc),
            bid=2.00,
            ask=2.50,
            last=2.45,
        )
        defaults.update(kwargs)
        return TickUpdate(**defaults)

    def test_valid_tick_constructs(self):
        t = self._make()
        assert t.con_id == 12345

    def test_con_id_zero_raises(self):
        with pytest.raises(ValidationError):
            self._make(con_id=0)

    def test_negative_con_id_raises(self):
        with pytest.raises(ValidationError):
            self._make(con_id=-1)

    def test_zero_strike_raises(self):
        with pytest.raises(ValidationError):
            self._make(strike=0.0)

    def test_negative_strike_raises(self):
        with pytest.raises(ValidationError):
            self._make(strike=-100.0)

    def test_empty_expiry_raises(self):
        with pytest.raises(ValidationError):
            self._make(expiry="")

    def test_invalid_right_raises(self):
        with pytest.raises(ValidationError):
            self._make(right="Z")

    def test_lowercase_right_normalised(self):
        t = self._make(right="p")
        assert t.right == "P"

    def test_out_of_range_iv_clamped_to_none(self):
        t = self._make(implied_vol=999.0)
        assert t.implied_vol is None

    def test_valid_iv_unchanged(self):
        t = self._make(implied_vol=0.30)
        assert t.implied_vol == pytest.approx(0.30)

    def test_out_of_range_delta_clamped_to_none(self):
        t = self._make(delta=-2.0)
        assert t.delta is None

    def test_valid_delta_unchanged(self):
        t = self._make(delta=-0.40)
        assert t.delta == pytest.approx(-0.40)

    def test_negative_volume_coerced_to_none(self):
        t = self._make(volume=-1)
        assert t.volume is None

    def test_negative_last_size_coerced_to_none(self):
        t = self._make(last_size=-10)
        assert t.last_size is None


# ===========================================================================
# _parse_ticker() gate behaviour
# ===========================================================================


def _make_ibkr_contract(
    symbol="SPY",
    expiry="20260320",
    strike=500.0,
    right="C",
    con_id=12345,
):
    """Build a minimal mock ib_insync contract."""
    c = MagicMock()
    c.symbol = symbol
    c.lastTradeDateOrContractMonth = expiry
    c.strike = strike
    c.right = right
    c.conId = con_id
    return c


def _make_ibkr_ticker(contract=None, bid=2.0, ask=2.5, last=2.45, greeks=None):
    """Build a minimal mock ib_insync Ticker."""
    ticker = MagicMock()
    ticker.contract = contract or _make_ibkr_contract()
    ticker.bid = bid
    ticker.ask = ask
    ticker.last = last
    ticker.volume = 100
    ticker.callOpenInterest = 1000
    ticker.putOpenInterest = 800
    ticker.modelGreeks = greeks
    return ticker


class TestParseTickerGate:
    def _fetcher(self):
        from src.data.chain_fetcher import ChainFetcher
        client = MagicMock()
        return ChainFetcher(client)

    def test_normal_ticker_parses_correctly(self):
        fetcher = self._fetcher()
        ticker = _make_ibkr_ticker()
        contract = fetcher._parse_ticker(ticker)
        assert contract.symbol == "SPY"
        assert contract.bid == pytest.approx(2.0)
        assert contract.ask == pytest.approx(2.5)

    def test_inverted_spread_clears_bid_and_ask(self):
        fetcher = self._fetcher()
        ticker = _make_ibkr_ticker(bid=5.0, ask=3.0)  # bid > ask
        contract = fetcher._parse_ticker(ticker)
        assert contract.bid is None
        assert contract.ask is None

    def test_validation_error_returns_minimal_fallback(self):
        """A ticker with an invalid expiry should return a minimal fallback contract."""
        fetcher = self._fetcher()
        bad_contract = _make_ibkr_contract(expiry="BADDATE")
        ticker = _make_ibkr_ticker(contract=bad_contract)
        # Should not raise — returns minimal fallback
        result = fetcher._parse_ticker(ticker)
        assert result.symbol == "SPY"
        assert result.bid is None
        assert result.ask is None
        assert result.implied_vol is None

    def test_no_price_data_logs_debug_but_returns_contract(self):
        """A ticker with all-None prices is unusual but still returned (chain completeness)."""
        fetcher = self._fetcher()
        import math
        ticker = _make_ibkr_ticker(bid=float("nan"), ask=float("nan"), last=float("nan"))
        # _clean() converts nan → None
        result = fetcher._parse_ticker(ticker)
        assert result is not None  # chain count preserved


# ===========================================================================
# _ticker_to_update() gate behaviour
# ===========================================================================


class TestTickerToUpdateGate:
    def _stream(self):
        from src.data.tick_stream import TickStream
        client = MagicMock()
        client.ib = MagicMock()
        stream = TickStream(client)
        # Pre-populate a subscription so the event handler can look up underlying_price
        stream._subscriptions[12345] = (MagicMock(), 500.0)
        return stream

    def _make_ticker(self, bid=2.0, ask=2.5, last=2.45, con_id=12345,
                     expiry="20260320", strike=500.0, right="C"):
        c = MagicMock()
        c.symbol = "SPY"
        c.conId = con_id
        c.lastTradeDateOrContractMonth = expiry
        c.strike = strike
        c.right = right
        ticker = MagicMock()
        ticker.contract = c
        ticker.bid = bid
        ticker.ask = ask
        ticker.last = last
        ticker.optVolume = 100
        ticker.optOpenInterest = 1000
        ticker.lastSize = 50
        ticker.bidSize = 10
        ticker.askSize = 8
        ticker.modelGreeks = None
        return ticker

    def test_normal_ticker_returns_update(self):
        stream = self._stream()
        ticker = self._make_ticker()
        result = stream._ticker_to_update(ticker, 500.0)
        assert result is not None
        assert result.symbol == "SPY"

    def test_all_prices_none_drops_tick(self):
        import math
        stream = self._stream()
        ticker = self._make_ticker(bid=float("nan"), ask=float("nan"), last=float("nan"))
        result = stream._ticker_to_update(ticker, 500.0)
        assert result is None

    def test_inverted_spread_clears_bid_ask_but_keeps_last(self):
        stream = self._stream()
        ticker = self._make_ticker(bid=5.0, ask=3.0, last=4.0)  # bid > ask
        result = stream._ticker_to_update(ticker, 500.0)
        assert result is not None
        assert result.bid is None
        assert result.ask is None
        assert result.last == pytest.approx(4.0)

    def test_validation_error_drops_tick(self):
        """A ticker with an invalid expiry should be dropped (returns None)."""
        stream = self._stream()
        ticker = self._make_ticker(expiry="BADDATE")
        result = stream._ticker_to_update(ticker, 500.0)
        assert result is None

    def test_missing_contract_returns_none(self):
        stream = self._stream()
        ticker = MagicMock()
        ticker.contract = None
        result = stream._ticker_to_update(ticker, 500.0)
        assert result is None
