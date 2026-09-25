"""Tests for the VentureBot local smoke-run entry point."""

from decimal import Decimal

import pytest

from venturebot.__main__ import run_smoke_test
from venturebot.database.connection import get_engine, get_session_factory
from venturebot.database.repositories.capital import CapitalRepository


def test_smoke_run_in_memory_offline():
    """Verify that run_smoke_test completes all stages cleanly in memory in offline mode."""
    exit_code = run_smoke_test(db_url="sqlite:///:memory:", offline=True)
    assert exit_code == 0


def test_smoke_run_capital_and_idempotency(tmp_path):
    """Verify that multiple smoke runs on the same database are strictly idempotent regarding initial capital."""
    db_file = tmp_path / "smoke_idempotent.db"
    db_url = f"sqlite:///{db_file}"

    # First run
    code1 = run_smoke_test(db_url=db_url, offline=True)
    assert code1 == 0

    engine = get_engine(db_url)
    session_factory = get_session_factory(engine)
    with session_factory() as session:
        repo = CapitalRepository(session)
        summary1 = repo.get_financial_summary()
        # Authoritative ledger assertions:
        assert summary1.starting_capital == Decimal("1000.00")
        assert summary1.total_revenue == Decimal("0.00")   # ZERO ledger revenue
        assert summary1.total_cost == Decimal("35.00")     # Actual ledger spend
        assert summary1.net_profit == Decimal("-35.00")    # Ledger profit is -35.00
        assert summary1.total_allocated == Decimal("0.00")
        assert summary1.available_unallocated == Decimal("965.00")
        assert summary1.current_balance == Decimal("965.00")

        # Verify NO REVENUE transactions exist on the capital ledger
        transactions = repo.get_transaction_history()
        revenue_txs = [tx for tx in transactions if tx.transaction_type.value == "revenue"]
        assert len(revenue_txs) == 0

    # Second run on existing database: initial deposit must NOT duplicate
    code2 = run_smoke_test(db_url=db_url, offline=True)
    assert code2 == 0

    with session_factory() as session:
        repo = CapitalRepository(session)
        summary2 = repo.get_financial_summary()
        # Starting capital remains 1000.00, spend increases by another 35.00 = 70.00
        assert summary2.starting_capital == Decimal("1000.00")
        assert summary2.total_revenue == Decimal("0.00")
        assert summary2.total_cost == Decimal("70.00")
        assert summary2.net_profit == Decimal("-70.00")
        assert summary2.total_allocated == Decimal("0.00")
        assert summary2.available_unallocated == Decimal("930.00")
        assert summary2.current_balance == Decimal("930.00")


def test_cli_nullable_financial_display_helpers():
    """Verify that CLI display helpers strictly distinguish unknown (None) from observed zero.

    Regression test for Step 44.4 / 44.5 contract:
    - revenue=None -> 'Unmeasured' (never crashes, never '0.00')
    - revenue=Decimal('0.00') -> 'INR 0.00' (explicit zero)
    - revenue=Decimal('396.00') -> 'INR 396.00'
    - profit_loss=None -> 'Unmeasured'
    - profit_loss=Decimal('0.00') -> 'INR 0.00'
    - conversion_rate=None -> 'Unmeasured' (never '0.00%')
    - conversion_rate=0.0 -> '0.00%'
    - roi=None -> 'Unmeasured' (never '0.0%')
    - roi=0.0 -> '0.0%'
    - roas=None -> 'Unmeasured' (never '0%')
    - roas=Decimal('2.50') -> '2.50x'
    """
    from venturebot.__main__ import _format_currency_obs, _format_percent_obs, _format_roas_obs

    # 1. Currency observations: revenue & profit_loss
    assert _format_currency_obs(None) == "Unmeasured"
    assert _format_currency_obs(Decimal("0.00")) == "INR 0.00"
    assert _format_currency_obs(Decimal("396.00")) == "INR 396.00"
    assert _format_currency_obs(Decimal("-35.00")) == "INR -35.00"
    assert _format_currency_obs(None, prefix="") == "Unmeasured"
    assert _format_currency_obs(Decimal("0.00"), prefix="") == "0.00"
    assert _format_currency_obs(Decimal("396.00"), prefix="") == "396.00"

    # 2. Percentage observations: conversion_rate & roi
    assert _format_percent_obs(None, precision=2) == "Unmeasured"
    assert _format_percent_obs(0.0, precision=2) == "0.00%"
    assert _format_percent_obs(0.1429, precision=2) == "14.29%"

    assert _format_percent_obs(None, precision=1) == "Unmeasured"
    assert _format_percent_obs(0.0, precision=1) == "0.0%"
    assert _format_percent_obs(10.3143, precision=1) == "1031.4%"
    assert _format_percent_obs(-1.0, precision=1) == "-100.0%"

    # 3. ROAS observations
    assert _format_roas_obs(None) == "Unmeasured"
    assert _format_roas_obs(Decimal("0.00")) == "0.00x"
    assert _format_roas_obs(Decimal("2.50")) == "2.50x"
