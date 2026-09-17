"""Tests for VentureBot data contracts (Step 2).

Tests cover: valid creation, required fields, invalid enums,
invalid monetary values, ID relationships, timestamps,
and the allocated_budget vs actual_spend distinction.
"""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from venturebot.models.capital import CapitalTransaction, TransactionType
from venturebot.models.decision import Decision, DecisionOutcome
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.metrics import ExperimentMetrics
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


# ── Opportunity ──────────────────────────────────────────────────────────────

def test_opportunity_valid_creation():
    opp = Opportunity(
        title="Sell Notion templates",
        description="Create and sell Notion productivity templates on Gumroad.",
        category=OpportunityCategory.PRODUCT,
    )
    assert opp.status == OpportunityStatus.DISCOVERED
    assert opp.confidence == 0.0
    assert opp.id is not None
    assert opp.created_at is not None


def test_opportunity_requires_title():
    with pytest.raises(ValidationError):
        Opportunity(description="x", category=OpportunityCategory.PRODUCT)  # type: ignore[call-arg]


def test_opportunity_requires_description():
    with pytest.raises(ValidationError):
        Opportunity(title="x", category=OpportunityCategory.PRODUCT)  # type: ignore[call-arg]


def test_opportunity_requires_category():
    with pytest.raises(ValidationError):
        Opportunity(title="x", description="y")  # type: ignore[call-arg]


def test_opportunity_invalid_category():
    with pytest.raises(ValidationError):
        Opportunity(title="x", description="y", category="flying_cars")  # type: ignore[arg-type]


def test_opportunity_invalid_status():
    with pytest.raises(ValidationError):
        Opportunity(title="x", description="y", category=OpportunityCategory.PRODUCT, status="vibes")  # type: ignore[arg-type]


def test_opportunity_confidence_bounds():
    with pytest.raises(ValidationError):
        Opportunity(title="x", description="y", category=OpportunityCategory.PRODUCT, confidence=1.5)
    with pytest.raises(ValidationError):
        Opportunity(title="x", description="y", category=OpportunityCategory.PRODUCT, confidence=-0.1)


def test_opportunity_monetary_defaults():
    opp = Opportunity(title="x", description="y", category=OpportunityCategory.CONTENT)
    assert opp.estimated_revenue_min == Decimal("0")
    assert opp.estimated_cost_max == Decimal("0")


# ── Experiment ───────────────────────────────────────────────────────────────

def _valid_experiment(**overrides) -> Experiment:
    defaults = dict(
        opportunity_id=uuid4(),
        hypothesis="Selling templates on Gumroad will generate ₹500 in 2 weeks.",
        objective="Revenue ≥ ₹500 in 14 days",
        channel=Channel.INSTAGRAM,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        allocated_budget=Decimal("200"),
        max_allowed_spend=Decimal("300"),
        success_criteria="Revenue ≥ ₹500",
        failure_criteria="Revenue < ₹50 after 7 days",
    )
    defaults.update(overrides)
    return Experiment(**defaults)


def test_experiment_valid_creation():
    exp = _valid_experiment()
    assert exp.status == ExperimentStatus.DRAFT
    assert exp.actual_spend == Decimal("0")
    assert exp.allocated_budget == Decimal("200")
    assert exp.max_allowed_spend == Decimal("300")


def test_experiment_budget_vs_spend_distinct():
    exp = _valid_experiment(actual_spend=Decimal("150"))
    assert exp.allocated_budget != exp.actual_spend
    assert exp.actual_spend == Decimal("150")


def test_experiment_actual_spend_exceeds_ceiling():
    with pytest.raises(ValidationError, match="max_allowed_spend"):
        _valid_experiment(actual_spend=Decimal("400"), max_allowed_spend=Decimal("300"))


def test_experiment_allocated_budget_exceeds_ceiling():
    with pytest.raises(ValidationError, match="max_allowed_spend"):
        _valid_experiment(allocated_budget=Decimal("500"), max_allowed_spend=Decimal("300"))


def test_experiment_negative_budget_rejected():
    with pytest.raises(ValidationError):
        _valid_experiment(allocated_budget=Decimal("-1"))


def test_experiment_invalid_channel():
    with pytest.raises(ValidationError):
        _valid_experiment(channel="tiktok_unofficial")  # type: ignore[arg-type]


def test_experiment_invalid_monetization():
    with pytest.raises(ValidationError):
        _valid_experiment(monetization_method="magic")  # type: ignore[arg-type]


def test_experiment_opportunity_id_required():
    with pytest.raises(ValidationError):
        Experiment(  # type: ignore[call-arg]
            hypothesis="h", objective="o", channel=Channel.EMAIL,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("100"), max_allowed_spend=Decimal("200"),
            success_criteria="s", failure_criteria="f",
        )


# ── ExperimentMetrics ─────────────────────────────────────────────────────────

def test_metrics_valid_creation():
    m = ExperimentMetrics(
        experiment_id=uuid4(),
        impressions=1000,
        clicks=50,
        revenue=Decimal("300"),
        cost=Decimal("200"),
        profit_loss=Decimal("100"),
    )
    assert m.revenue == Decimal("300")
    assert m.profit_loss == Decimal("100")


def test_metrics_all_optional_fields():
    m = ExperimentMetrics(experiment_id=uuid4())
    assert m.impressions is None
    assert m.roas is None
    assert m.roi is None
    assert m.revenue == Decimal("0")


def test_metrics_negative_revenue_rejected():
    with pytest.raises(ValidationError):
        ExperimentMetrics(experiment_id=uuid4(), revenue=Decimal("-1"))


def test_metrics_negative_cost_rejected():
    with pytest.raises(ValidationError):
        ExperimentMetrics(experiment_id=uuid4(), cost=Decimal("-50"))


def test_metrics_conversion_rate_out_of_range():
    with pytest.raises(ValidationError):
        ExperimentMetrics(experiment_id=uuid4(), conversion_rate=1.5)
    with pytest.raises(ValidationError):
        ExperimentMetrics(experiment_id=uuid4(), conversion_rate=-0.01)


def test_metrics_experiment_id_required():
    with pytest.raises(ValidationError):
        ExperimentMetrics()  # type: ignore[call-arg]


# ── CapitalTransaction ───────────────────────────────────────────────────────

def test_capital_valid_creation():
    tx = CapitalTransaction(
        transaction_type=TransactionType.INITIAL_DEPOSIT,
        amount=Decimal("1000"),
        description="Starting capital ₹1,000",
    )
    assert tx.amount == Decimal("1000")
    assert tx.experiment_id is None


def test_capital_with_experiment_id():
    exp_id = uuid4()
    tx = CapitalTransaction(
        experiment_id=exp_id,
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("150"),
        description="Ad spend for template experiment",
    )
    assert tx.experiment_id == exp_id


def test_capital_zero_amount_rejected():
    with pytest.raises(ValidationError):
        CapitalTransaction(
            transaction_type=TransactionType.REVENUE,
            amount=Decimal("0"),
            description="zero amount",
        )


def test_capital_negative_amount_rejected():
    with pytest.raises(ValidationError):
        CapitalTransaction(
            transaction_type=TransactionType.REVENUE,
            amount=Decimal("-10"),
            description="negative",
        )


def test_capital_invalid_transaction_type():
    with pytest.raises(ValidationError):
        CapitalTransaction(
            transaction_type="magic_money",  # type: ignore[arg-type]
            amount=Decimal("100"),
            description="bad type",
        )


def test_capital_description_required():
    with pytest.raises(ValidationError):
        CapitalTransaction(transaction_type=TransactionType.REVENUE, amount=Decimal("100"))  # type: ignore[call-arg]


# ── Decision ─────────────────────────────────────────────────────────────────

def test_decision_valid_kill():
    d = Decision(outcome=DecisionOutcome.KILL, reason="No conversions after 7 days.")
    assert d.outcome == DecisionOutcome.KILL
    assert d.opportunity_id is None
    assert d.experiment_id is None


def test_decision_valid_scale():
    exp_id = uuid4()
    d = Decision(
        experiment_id=exp_id,
        outcome=DecisionOutcome.SCALE,
        reason="ROAS 4x, increase budget.",
        confidence=0.85,
    )
    assert d.confidence == 0.85
    assert d.experiment_id == exp_id


def test_decision_outcome_required():
    with pytest.raises(ValidationError):
        Decision(reason="no outcome")  # type: ignore[call-arg]


def test_decision_reason_required():
    with pytest.raises(ValidationError):
        Decision(outcome=DecisionOutcome.HOLD)  # type: ignore[call-arg]


def test_decision_invalid_outcome():
    with pytest.raises(ValidationError):
        Decision(outcome="maybe", reason="unsure")  # type: ignore[arg-type]


def test_decision_confidence_bounds():
    with pytest.raises(ValidationError):
        Decision(outcome=DecisionOutcome.ITERATE, reason="x", confidence=1.1)
    with pytest.raises(ValidationError):
        Decision(outcome=DecisionOutcome.ITERATE, reason="x", confidence=-0.5)


def test_decision_all_outcomes_valid():
    for outcome in DecisionOutcome:
        d = Decision(outcome=outcome, reason="test")
        assert d.outcome == outcome


# ── Relational ID checks ─────────────────────────────────────────────────────

def test_experiment_links_to_opportunity():
    opp_id = uuid4()
    exp = _valid_experiment(opportunity_id=opp_id)
    assert exp.opportunity_id == opp_id


def test_metrics_links_to_experiment():
    exp_id = uuid4()
    m = ExperimentMetrics(experiment_id=exp_id)
    assert m.experiment_id == exp_id


def test_capital_links_to_experiment():
    exp_id = uuid4()
    tx = CapitalTransaction(
        experiment_id=exp_id,
        transaction_type=TransactionType.EXPERIMENT_ALLOCATION,
        amount=Decimal("200"),
        description="Budget allocation",
    )
    assert tx.experiment_id == exp_id


def test_decision_links_to_opportunity():
    opp_id = uuid4()
    d = Decision(opportunity_id=opp_id, outcome=DecisionOutcome.REJECT if hasattr(DecisionOutcome, "REJECT") else DecisionOutcome.KILL, reason="too competitive")
    assert d.opportunity_id == opp_id
