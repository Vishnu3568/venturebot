"""Unit tests for Opportunity Evaluation Foundation (Step 5).

Verifies:
- Valid opportunity deterministic evaluation
- Missing evidence and incomplete feasibility detection
- Insufficient economics detection
- Capital risk boundary enforcement (against starting capital ₹1,000)
- Risk flag detection (platform dependency, regulatory, high competition)
- Input immutability
- Absolute isolation from financial ledger and persistent database
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.evaluation.evaluator import OpportunityEvaluator
from venturebot.evaluation.models import EvaluationStatus
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


@pytest.fixture
def sample_ready_opportunity() -> Opportunity:
    return Opportunity(
        title="Automated Excel cleanup for accountants",
        description="Python macro/script packaging for cleaning tabular ledger dumps.",
        category=OpportunityCategory.PRODUCT,
        status=OpportunityStatus.DISCOVERED,
        source="Reddit /r/accounting thread #1234",
        evidence_notes="High frequency complaints on manual reconciliation time.",
        audience="Small accounting firms in India",
        trend_strength="Growing search volume on reconciliation automation",
        growth_indicators="Monthly keyword growth +18%",
        competition_level="Moderate",
        monetization_notes="One-time purchase license at ₹499",
        production_difficulty="Low - Python script wrapper",
        distribution_difficulty="Low - Direct outreach on LinkedIn",
        automation_potential="High",
        platform_dependency="Excel desktop",
        regulatory_notes="None",
        estimated_revenue_min=Decimal("500.00"),
        estimated_revenue_max=Decimal("2000.00"),
        estimated_cost_min=Decimal("50.00"),
        estimated_cost_max=Decimal("150.00"),
        confidence=0.8,
    )


def test_valid_opportunity_evaluation_ready(sample_ready_opportunity: Opportunity):
    """Complete opportunity with evidence and economics evaluates to READY_FOR_EXPERIMENT_DESIGN."""
    result = OpportunityEvaluator.evaluate(sample_ready_opportunity)

    assert result.opportunity_id == sample_ready_opportunity.id
    assert result.status == EvaluationStatus.READY_FOR_EXPERIMENT_DESIGN
    assert result.has_source_evidence is True
    assert result.has_economic_estimates is True
    assert result.exceeds_starting_capital_risk is False
    assert result.completeness_ratio == 1.0
    assert len(result.missing_dimensions) == 0
    assert result.estimated_margin_min == Decimal("350.00")  # 500 - 150
    assert result.estimated_margin_max == Decimal("1950.00")  # 2000 - 50


def test_deterministic_evaluation(sample_ready_opportunity: Opportunity):
    """Evaluating the same opportunity multiple times yields identical results."""
    res1 = OpportunityEvaluator.evaluate(sample_ready_opportunity)
    res2 = OpportunityEvaluator.evaluate(sample_ready_opportunity)

    assert res1.status == res2.status
    assert res1.completeness_ratio == res2.completeness_ratio
    assert res1.estimated_margin_min == res2.estimated_margin_min
    assert res1.estimated_margin_max == res2.estimated_margin_max
    assert res1.risk_flags == res2.risk_flags


def test_missing_evidence_evaluated_as_needs_evidence():
    """Opportunity without source or evidence notes evaluates to NEEDS_EVIDENCE."""
    opp = Opportunity(
        title="Unverified Idea",
        description="Just a thought with no research",
        category=OpportunityCategory.PRODUCT,
        audience="General public",
        monetization_notes="Sell online",
        estimated_revenue_max=Decimal("1000.00"),
        estimated_cost_max=Decimal("100.00"),
    )
    result = OpportunityEvaluator.evaluate(opp)

    assert result.status == EvaluationStatus.NEEDS_EVIDENCE
    assert result.has_source_evidence is False
    assert "Missing verified source or evidence references" in result.risk_flags


def test_insufficient_economics_evaluated():
    """Opportunity with zero revenue and cost estimates evaluates to INSUFFICIENT_ECONOMICS."""
    opp = Opportunity(
        title="Idea with evidence but no economics",
        description="Valid research but no cost/revenue thoughts",
        category=OpportunityCategory.SERVICE,
        source="Industry report 2026",
        audience="Designers",
        monetization_notes="Hourly consulting",
    )
    result = OpportunityEvaluator.evaluate(opp)

    assert result.status == EvaluationStatus.INSUFFICIENT_ECONOMICS
    assert result.has_economic_estimates is False


def test_exceeds_starting_capital_risk():
    """Opportunity requiring more minimum capital than the ₹1,000 pool is flagged."""
    opp = Opportunity(
        title="Capital heavy venture",
        description="Requires server cluster",
        category=OpportunityCategory.PRODUCT,
        source="Cloud architecture review",
        audience="Enterprises",
        monetization_notes="SaaS subscription",
        estimated_cost_min=Decimal("2500.00"),  # Exceeds ₹1,000 starting pool
        estimated_cost_max=Decimal("5000.00"),
        estimated_revenue_max=Decimal("10000.00"),
    )
    result = OpportunityEvaluator.evaluate(opp)

    assert result.status == EvaluationStatus.EXCEEDS_CAPITAL_LIMIT
    assert result.exceeds_starting_capital_risk is True
    assert any("exceeds available starting capital" in f for f in result.risk_flags)


def test_incomplete_feasibility_evaluated():
    """Opportunity missing key feasibility dimensions evaluates to INCOMPLETE."""
    opp = Opportunity(
        title="Incomplete Idea",
        description="Has evidence and estimates but no audience or monetization defined",
        category=OpportunityCategory.CONTENT,
        source="Google Trends data",
        estimated_revenue_max=Decimal("500.00"),
        estimated_cost_max=Decimal("50.00"),
    )
    result = OpportunityEvaluator.evaluate(opp)

    assert result.status == EvaluationStatus.INCOMPLETE
    assert result.completeness_ratio < 1.0


def test_risk_flags_detection():
    """Platform dependency, regulatory notes, and high competition are flagged."""
    opp = Opportunity(
        title="Shopify plugin for medical clinics",
        description="Plugin for clinic scheduling",
        category=OpportunityCategory.PRODUCT,
        source="Shopify App Store search",
        audience="Clinic owners",
        monetization_notes="Monthly subscription",
        platform_dependency="Shopify App Store APIs",
        regulatory_notes="Healthcare data compliance requirements (HIPAA/DISHA)",
        competition_level="High",
        estimated_revenue_max=Decimal("1000.00"),
        estimated_cost_max=Decimal("200.00"),
    )
    result = OpportunityEvaluator.evaluate(opp)

    assert any("Platform lock-in risk noted" in f for f in result.risk_flags)
    assert any("Regulatory/policy risk noted" in f for f in result.risk_flags)
    assert any("High competition level flagged" in f for f in result.risk_flags)


def test_evaluation_does_not_mutate_input(sample_ready_opportunity: Opportunity):
    """Evaluation leaves the input Opportunity object completely unchanged."""
    title_before = sample_ready_opportunity.title
    status_before = sample_ready_opportunity.status

    _ = OpportunityEvaluator.evaluate(sample_ready_opportunity)

    assert sample_ready_opportunity.title == title_before
    assert sample_ready_opportunity.status == status_before


def test_evaluation_does_not_affect_financial_ledger_or_database(sample_ready_opportunity: Opportunity):
    """Evaluation runs in total isolation from the financial ledger and database."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)

    with session_factory() as session:
        cap_repo = CapitalRepository(session)
        cap_repo.initialize_starting_capital()
        balance_before = cap_repo.get_current_balance()
        history_len_before = len(cap_repo.get_transaction_history())

        # Perform evaluation
        _ = OpportunityEvaluator.evaluate(sample_ready_opportunity)

        # Confirm ledger is 100% untouched
        assert cap_repo.get_current_balance() == balance_before
        assert len(cap_repo.get_transaction_history()) == history_len_before
