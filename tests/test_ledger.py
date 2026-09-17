"""Unit and integration tests for VentureBot financial ledger (Step 3).

Verifies:
- Initialization (fresh ledger ₹1,000, no duplicate initial capital)
- Transaction validity (income, expense, experiment spend, revenue, invalid amount/type)
- Balance calculation (starting + revenue - expense = ₹1,050 example)
- Profit / loss calculation (profit ₹50, loss -₹60 examples)
- Auditability (append-only, immutable history)
- Test isolation using SQLite in-memory databases
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.models.capital import CapitalTransaction, TransactionType


@pytest.fixture
def session() -> Session:
    """Provide an isolated, clean in-memory SQLite database session for each test."""
    # Using a unique in-memory database per test ensures total test isolation
    db_url = f"sqlite:///:memory:"
    engine = get_engine(db_url)
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture
def repo(session: Session) -> CapitalRepository:
    """Provide a CapitalRepository instance bound to the isolated session."""
    return CapitalRepository(session)


# ── 1. Initialization Tests ───────────────────────────────────────────────────

def test_fresh_ledger_initializes_with_one_thousand(repo: CapitalRepository):
    """Fresh ledger starts with ₹1,000 upon initialization."""
    tx = repo.initialize_starting_capital()
    assert tx.transaction_type == TransactionType.INITIAL_DEPOSIT
    assert tx.amount == Decimal("1000.00")
    assert tx.description == "Initial starting capital ₹1,000.00"

    balance = repo.get_current_balance()
    assert balance == Decimal("1000.00")

    history = repo.get_transaction_history()
    assert len(history) == 1
    assert history[0].id == tx.id


def test_initialization_cannot_duplicate_starting_capital(repo: CapitalRepository):
    """Calling initialization multiple times returns existing transaction without duplicating."""
    tx1 = repo.initialize_starting_capital()
    tx2 = repo.initialize_starting_capital()

    assert tx1.id == tx2.id
    history = repo.get_transaction_history()
    assert len(history) == 1
    assert repo.get_current_balance() == Decimal("1000.00")


def test_direct_duplicate_initial_deposit_rejected(repo: CapitalRepository):
    """Attempting to record a second INITIAL_DEPOSIT transaction raises ValueError."""
    repo.initialize_starting_capital()

    second_deposit = CapitalTransaction(
        transaction_type=TransactionType.INITIAL_DEPOSIT,
        amount=Decimal("500.00"),
        description="Illegal second deposit",
    )
    with pytest.raises(ValueError, match="Initial capital has already been deposited"):
        repo.record_transaction(second_deposit)


# ── 2. Transaction Validity Tests ─────────────────────────────────────────────

def test_valid_income_transaction(repo: CapitalRepository):
    """Income transaction (revenue) increases inflow and current balance."""
    repo.initialize_starting_capital()
    tx = repo.record_transaction(
        transaction_type=TransactionType.REVENUE,
        amount=Decimal("250.00"),
        description="Affiliate commission earned",
    )
    assert tx.amount == Decimal("250.00")
    assert tx.transaction_type == TransactionType.REVENUE
    assert repo.get_current_balance() == Decimal("1250.00")


def test_valid_expense_transaction(repo: CapitalRepository):
    """General expense transaction (e.g. withdrawal) records correctly."""
    repo.initialize_starting_capital()
    tx = repo.record_transaction(
        transaction_type=TransactionType.WITHDRAWAL,
        amount=Decimal("100.00"),
        description="Owner capital withdrawal",
    )
    assert tx.amount == Decimal("100.00")
    assert tx.transaction_type == TransactionType.WITHDRAWAL
    assert repo.get_current_balance() == Decimal("900.00")


def test_valid_experiment_expense(repo: CapitalRepository):
    """Experiment expense links to an experiment_id and updates cost."""
    repo.initialize_starting_capital()
    exp_id = uuid4()
    tx = repo.record_transaction(
        experiment_id=exp_id,
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("75.50"),
        description="Domain registration for experiment",
    )
    assert tx.experiment_id == exp_id
    assert tx.transaction_type == TransactionType.EXPERIMENT_SPEND
    assert tx.amount == Decimal("75.50")

    summary = repo.get_financial_summary()
    assert summary.total_cost == Decimal("75.50")
    assert summary.total_experiment_spending == Decimal("75.50")
    assert summary.current_balance == Decimal("924.50")


def test_valid_experiment_revenue(repo: CapitalRepository):
    """Experiment revenue links to an experiment_id and updates total revenue."""
    repo.initialize_starting_capital()
    exp_id = uuid4()
    tx = repo.record_transaction(
        experiment_id=exp_id,
        transaction_type=TransactionType.REVENUE,
        amount=Decimal("120.00"),
        description="Gumroad sale from experiment",
    )
    assert tx.experiment_id == exp_id
    assert tx.transaction_type == TransactionType.REVENUE

    summary = repo.get_financial_summary()
    assert summary.total_revenue == Decimal("120.00")
    assert summary.current_balance == Decimal("1120.00")


def test_invalid_amount_rejected_zero(repo: CapitalRepository):
    """Zero amount transaction is rejected."""
    with pytest.raises(ValidationError):
        repo.record_transaction(
            transaction_type=TransactionType.REVENUE,
            amount=Decimal("0.00"),
            description="Zero amount test",
        )


def test_invalid_amount_rejected_negative(repo: CapitalRepository):
    """Negative amount transaction is rejected."""
    with pytest.raises(ValidationError):
        repo.record_transaction(
            transaction_type=TransactionType.EXPERIMENT_SPEND,
            amount=Decimal("-50.00"),
            description="Negative amount test",
        )


def test_invalid_transaction_type_rejected(repo: CapitalRepository):
    """Invalid transaction type string is rejected."""
    with pytest.raises(ValidationError):
        repo.record_transaction(
            transaction_type="invalid_type",  # type: ignore[arg-type]
            amount=Decimal("100.00"),
            description="Bad type test",
        )


def test_empty_description_rejected(repo: CapitalRepository):
    """Missing or empty description is rejected."""
    with pytest.raises(ValidationError):
        repo.record_transaction(
            transaction_type=TransactionType.REVENUE,
            amount=Decimal("100.00"),
        )


# ── 3. Balance Calculation Example ───────────────────────────────────────────

def test_balance_calculation_example(repo: CapitalRepository):
    """
    Prompt requirement example:
    Initial capital = ₹1,000
    Expense = ₹100
    Revenue = ₹150
    Expected current balance: ₹1,050
    """
    repo.initialize_starting_capital()  # ₹1,000
    repo.record_transaction(
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("100.00"),
        description="Ad test expense",
    )
    repo.record_transaction(
        transaction_type=TransactionType.REVENUE,
        amount=Decimal("150.00"),
        description="Customer payments",
    )

    summary = repo.get_financial_summary()
    assert summary.starting_capital == Decimal("1000.00")
    assert summary.total_cost == Decimal("100.00")
    assert summary.total_revenue == Decimal("150.00")
    assert summary.total_inflow == Decimal("1150.00")  # 1000 + 150
    assert summary.total_outflow == Decimal("100.00")  # 100
    assert summary.current_balance == Decimal("1050.00")
    assert repo.get_current_balance() == Decimal("1050.00")


# ── 4. Profit / Loss Calculation Examples ─────────────────────────────────────

def test_profit_calculation_example(repo: CapitalRepository):
    """
    Prompt requirement example:
    Cost = ₹100
    Revenue = ₹150
    Expected profit: ₹50
    """
    repo.initialize_starting_capital()
    repo.record_transaction(
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("100.00"),
        description="Experiment costs",
    )
    repo.record_transaction(
        transaction_type=TransactionType.REVENUE,
        amount=Decimal("150.00"),
        description="Experiment sales",
    )

    summary = repo.get_financial_summary()
    assert summary.net_profit == Decimal("50.00")
    assert summary.total_profit_loss == Decimal("50.00")
    assert summary.roi == 0.5  # 50 / 100 = 50% ROI


def test_loss_calculation_example(repo: CapitalRepository):
    """
    Prompt requirement example:
    Cost = ₹100
    Revenue = ₹40
    Expected profit: -₹60
    """
    repo.initialize_starting_capital()
    repo.record_transaction(
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("100.00"),
        description="Failed experiment costs",
    )
    repo.record_transaction(
        transaction_type=TransactionType.REVENUE,
        amount=Decimal("40.00"),
        description="Partial recovery revenue",
    )

    summary = repo.get_financial_summary()
    assert summary.net_profit == Decimal("-60.00")
    assert summary.total_profit_loss == Decimal("-60.00")
    assert summary.roi == -0.6  # -60 / 100 = -60% ROI


def test_zero_cost_roi_is_none(repo: CapitalRepository):
    """When no costs have been incurred, ROI is None (avoids division by zero)."""
    repo.initialize_starting_capital()
    summary = repo.get_financial_summary()
    assert summary.total_cost == Decimal("0.00")
    assert summary.roi is None


# ── 5. Auditability & Immutability Tests ──────────────────────────────────────

def test_transactions_are_append_only_and_immutable(repo: CapitalRepository):
    """Recording new transactions does not mutate or erase previous records."""
    tx1 = repo.initialize_starting_capital()
    tx2 = repo.record_transaction(
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("50.00"),
        description="First spend",
    )
    tx3 = repo.record_transaction(
        transaction_type=TransactionType.REVENUE,
        amount=Decimal("80.00"),
        description="First revenue",
    )

    history = repo.get_transaction_history()
    assert len(history) == 3

    # Historical record tx1 is intact
    fetched_tx1 = repo.get_transaction(tx1.id)
    assert fetched_tx1 is not None
    assert fetched_tx1.id == tx1.id
    assert fetched_tx1.amount == tx1.amount
    assert fetched_tx1.recorded_at == tx1.recorded_at
    assert fetched_tx1.description == tx1.description

    # Historical record tx2 is intact
    fetched_tx2 = repo.get_transaction(tx2.id)
    assert fetched_tx2 is not None
    assert fetched_tx2.id == tx2.id
    assert fetched_tx2.amount == Decimal("50.00")


def test_transaction_history_filtering(repo: CapitalRepository):
    """Filtering by experiment_id and transaction_type works correctly."""
    repo.initialize_starting_capital()
    exp_a = uuid4()
    exp_b = uuid4()

    repo.record_transaction(
        experiment_id=exp_a,
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("100.00"),
        description="Exp A spend",
    )
    repo.record_transaction(
        experiment_id=exp_b,
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("200.00"),
        description="Exp B spend",
    )
    repo.record_transaction(
        experiment_id=exp_a,
        transaction_type=TransactionType.REVENUE,
        amount=Decimal("300.00"),
        description="Exp A revenue",
    )

    # Filter by experiment_id
    exp_a_history = repo.get_transaction_history(experiment_id=exp_a)
    assert len(exp_a_history) == 2
    assert all(t.experiment_id == exp_a for t in exp_a_history)

    # Filter by transaction_type
    revenue_history = repo.get_transaction_history(transaction_type=TransactionType.REVENUE)
    assert len(revenue_history) == 1
    assert revenue_history[0].amount == Decimal("300.00")


# ── 6. Test Isolation ─────────────────────────────────────────────────────────

def test_session_isolation_independent(session: Session):
    """Verify that multiple repositories on clean sessions have independent data."""
    repo1 = CapitalRepository(session)
    repo1.initialize_starting_capital()
    assert len(repo1.get_transaction_history()) == 1

    # Second isolated in-memory DB has 0 transactions
    engine2 = get_engine("sqlite:///:memory:")
    init_db(engine2)
    session_factory2 = get_session_factory(engine2)
    with session_factory2() as session2:
        repo2 = CapitalRepository(session2)
        assert len(repo2.get_transaction_history()) == 0
        assert repo2.get_current_balance() == Decimal("0.00")
