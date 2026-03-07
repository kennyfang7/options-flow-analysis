from __future__ import annotations


def test_chain_snapshot_tablename():
    from src.storage.models import ChainSnapshot
    assert ChainSnapshot.__tablename__ == "chain_snapshots"


def test_option_contract_record_tablename():
    from src.storage.models import OptionContractRecord
    assert OptionContractRecord.__tablename__ == "option_contracts"


def test_option_tick_tablename():
    from src.storage.models import OptionTick
    assert OptionTick.__tablename__ == "option_ticks"


def test_chain_snapshot_columns():
    from src.storage.models import ChainSnapshot
    cols = {c.name for c in ChainSnapshot.__table__.columns}
    assert cols == {"id", "underlying", "underlying_price", "captured_at"}


def test_option_contract_record_columns():
    from src.storage.models import OptionContractRecord
    cols = {c.name for c in OptionContractRecord.__table__.columns}
    expected = {
        "id", "snapshot_id", "symbol", "expiry", "strike", "right", "con_id",
        "bid", "ask", "last", "volume", "open_interest",
        "implied_vol", "delta", "gamma", "theta", "vega",
    }
    assert cols == expected


def test_option_tick_columns():
    from src.storage.models import OptionTick
    cols = {c.name for c in OptionTick.__table__.columns}
    expected = {
        "id", "symbol", "con_id", "expiry", "strike", "right", "received_at",
        "bid", "ask", "last", "volume", "open_interest",
        "last_size", "bid_size", "ask_size", "underlying_price",
        "implied_vol", "delta", "gamma", "theta", "vega",
    }
    assert cols == expected


def test_option_contract_record_unique_constraint():
    from src.storage.models import OptionContractRecord
    constraint_names = {c.name for c in OptionContractRecord.__table__.constraints}
    assert "uq_snapshot_contract" in constraint_names


def test_option_ticks_has_con_id_index():
    from src.storage.models import OptionTick
    index_names = {i.name for i in OptionTick.__table__.indexes}
    assert "ix_option_ticks_con_id_received_at" in index_names


def test_chain_snapshots_has_underlying_index():
    from src.storage.models import ChainSnapshot
    index_names = {i.name for i in ChainSnapshot.__table__.indexes}
    assert "ix_chain_snapshots_underlying_captured_at" in index_names


def test_option_ticks_has_symbol_index():
    from src.storage.models import OptionTick
    index_names = {i.name for i in OptionTick.__table__.indexes}
    assert "ix_option_ticks_symbol_received_at" in index_names
