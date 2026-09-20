"""Unit and integration tests for VentureBot core persistence layer (Step 4).

Verifies:
- Creating, retrieving, and listing Opportunities
- Opportunity status filtering and lifecycle updates
- Creating, retrieving, and listing Experiments linked to Opportunities
- Preserving Opportunity -> Experiment relationships and cascade/FK integrity
- Creating, retrieving, and listing ExperimentMetrics linked to Experiments
- Creating, retrieving, and listing Decisions linked to Opportunities/Experiments
- Preserving the CapitalTransaction financial ledger as the authoritative source
  of truth for Experiment actual_spend
- Database isolation between tests
"""

from collections.abc import Generator
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.models.capital import TransactionType
from venturebot.models.decision import Decision, DecisionOutcome
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.metrics import ExperimentMetrics
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Provide an isolated in-memory SQLite session with foreign keys enabled."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


# ── 1. Opportunity Repository Tests ──────────────────────────────────────────

def test_create_and_retrieve_opportunity(session: Session):
    repo = OpportunityRepository(session)
    opp = Opportunity(
        title="Micro SaaS for Excel formatting",
        description="Automated tool for formatting financial Excel files.",
        category=OpportunityCategory.PRODUCT,
        source="Reddit /r/excel",
        audience="Finance analysts",
        confidence=0.75,
        estimated_revenue_min=Decimal("500.00"),
        estimated_revenue_max=Decimal("2000.00"),
    )
    saved = repo.create(opp)
    assert saved.id == opp.id
    assert saved.title == opp.title
    assert saved.status == OpportunityStatus.DISCOVERED
    assert saved.confidence == 0.75

    fetched = repo.get(opp.id)
    assert fetched is not None
    assert fetched.id == opp.id
    assert fetched.category == OpportunityCategory.PRODUCT
    assert fetched.estimated_revenue_min == Decimal("500.00")


def test_list_and_filter_opportunities(session: Session):
    repo = OpportunityRepository(session)
    opp1 = Opportunity(
        title="Resume Review Service",
        description="Review engineering resumes",
        category=OpportunityCategory.SERVICE,
        status=OpportunityStatus.DISCOVERED,
    )
    opp2 = Opportunity(
        title="Notion Template Pack",
        description="Productivity templates",
        category=OpportunityCategory.PRODUCT,
        status=OpportunityStatus.APPROVED,
    )
    repo.create(opp1)
    repo.create(opp2)

    all_opps = repo.list()
    assert len(all_opps) == 2

    product_opps = repo.list(category=OpportunityCategory.PRODUCT)
    assert len(product_opps) == 1
    assert product_opps[0].id == opp2.id

    approved_opps = repo.list(status=OpportunityStatus.APPROVED)
    assert len(approved_opps) == 1
    assert approved_opps[0].id == opp2.id


def test_update_opportunity_status(session: Session):
    repo = OpportunityRepository(session)
    opp = Opportunity(
        title="Design assets",
        description="Icons",
        category=OpportunityCategory.PRODUCT,
    )
    repo.create(opp)

    updated = repo.update_status(opp.id, OpportunityStatus.APPROVED)
    assert updated is not None
    assert updated.status == OpportunityStatus.APPROVED

    fetched = repo.get(opp.id)
    assert fetched is not None
    assert fetched.status == OpportunityStatus.APPROVED


# ── 2. Experiment Repository Tests ───────────────────────────────────────────

def test_create_and_retrieve_experiment_with_relationship(session: Session):
    opp_repo = OpportunityRepository(session)
    exp_repo = ExperimentRepository(session)

    opp = Opportunity(
        title="Newsletter sponsorship test",
        description="Weekly tech curation",
        category=OpportunityCategory.CONTENT,
    )
    opp_repo.create(opp)

    exp = Experiment(
        opportunity_id=opp.id,
        hypothesis="A tech newsletter will generate ₹300 from sponsor clicks.",
        objective="Achieve 50 clicks in 7 days",
        channel=Channel.EMAIL,
        monetization_method=MonetizationMethod.SPONSORSHIP,
        allocated_budget=Decimal("150.00"),
        max_allowed_spend=Decimal("200.00"),
        success_criteria="Clicks >= 50",
        failure_criteria="Clicks < 10",
    )
    saved_exp = exp_repo.create(exp)
    assert saved_exp.id == exp.id
    assert saved_exp.opportunity_id == opp.id
    assert saved_exp.status == ExperimentStatus.DRAFT

    fetched = exp_repo.get(exp.id)
    assert fetched is not None
    assert fetched.opportunity_id == opp.id
    assert fetched.allocated_budget == Decimal("150.00")
    assert fetched.max_allowed_spend == Decimal("200.00")


def test_experiment_foreign_key_enforced(session: Session):
    """Attempting to create an Experiment with non-existent Opportunity ID fails FK constraint."""
    exp_repo = ExperimentRepository(session)
    exp = Experiment(
        opportunity_id=uuid4(),  # Random non-existent ID
        hypothesis="Will fail FK",
        objective="Fail test",
        channel=Channel.WEBSITE,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        allocated_budget=Decimal("50.00"),
        max_allowed_spend=Decimal("100.00"),
        success_criteria="None",
        failure_criteria="None",
    )
    with pytest.raises(IntegrityError):
        exp_repo.create(exp)


def test_list_experiments_by_opportunity(session: Session):
    opp_repo = OpportunityRepository(session)
    exp_repo = ExperimentRepository(session)

    opp1 = opp_repo.create(Opportunity(title="Opp 1", description="d1", category=OpportunityCategory.PRODUCT))
    opp2 = opp_repo.create(Opportunity(title="Opp 2", description="d2", category=OpportunityCategory.SERVICE))

    exp1 = exp_repo.create(Experiment(
        opportunity_id=opp1.id,
        hypothesis="h1",
        objective="o1",
        channel=Channel.INSTAGRAM,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        allocated_budget=Decimal("100.00"),
        max_allowed_spend=Decimal("100.00"),
        success_criteria="s1",
        failure_criteria="f1",
    ))
    exp2 = exp_repo.create(Experiment(
        opportunity_id=opp2.id,
        hypothesis="h2",
        objective="o2",
        channel=Channel.LINKEDIN,
        monetization_method=MonetizationMethod.FREELANCE,
        allocated_budget=Decimal("100.00"),
        max_allowed_spend=Decimal("100.00"),
        success_criteria="s2",
        failure_criteria="f2",
    ))

    opp1_exps = exp_repo.list(opportunity_id=opp1.id)
    assert len(opp1_exps) == 1
    assert opp1_exps[0].id == exp1.id


# ── 3. Financial Source-of-Truth Preservation ────────────────────────────────

def test_experiment_actual_spend_sourced_from_capital_ledger(session: Session):
    """
    Verifies that the CapitalTransaction ledger is the single source of truth for
    Experiment actual_spend. Recording an EXPERIMENT_SPEND transaction automatically
    hydrates the experiment's actual_spend.
    """
    opp_repo = OpportunityRepository(session)
    exp_repo = ExperimentRepository(session)
    cap_repo = CapitalRepository(session)

    cap_repo.initialize_starting_capital()

    opp = opp_repo.create(Opportunity(title="Ad test", description="desc", category=OpportunityCategory.PRODUCT))
    exp = exp_repo.create(Experiment(
        opportunity_id=opp.id,
        hypothesis="Ads will convert",
        objective="10 sales",
        channel=Channel.FACEBOOK,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("300.00"),
        success_criteria="Sales >= 10",
        failure_criteria="Sales < 2",
    ))

    # Before spend: actual_spend is 0
    fetched_exp = exp_repo.get(exp.id)
    assert fetched_exp is not None
    assert fetched_exp.actual_spend == Decimal("0.00")

    # Record spend in the capital ledger
    cap_repo.record_transaction(
        experiment_id=exp.id,
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("125.50"),
        description="Facebook ad creative test",
    )

    # Re-fetch experiment: actual_spend is computed from the ledger
    updated_exp = exp_repo.get(exp.id)
    assert updated_exp is not None
    assert updated_exp.actual_spend == Decimal("125.50")

    # Record a second spend
    cap_repo.record_transaction(
        experiment_id=exp.id,
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("50.00"),
        description="Follow-up ad spend",
    )

    # Re-fetch: cumulative actual spend is 175.50
    updated_exp2 = exp_repo.get(exp.id)
    assert updated_exp2 is not None
    assert updated_exp2.actual_spend == Decimal("175.50")


# ── 4. Experiment Metrics Repository Tests ───────────────────────────────────

def test_create_and_retrieve_metrics(session: Session):
    opp_repo = OpportunityRepository(session)
    exp_repo = ExperimentRepository(session)
    metrics_repo = MetricsRepository(session)

    opp = opp_repo.create(Opportunity(title="Opp", description="d", category=OpportunityCategory.PRODUCT))
    exp = exp_repo.create(Experiment(
        opportunity_id=opp.id,
        hypothesis="h",
        objective="o",
        channel=Channel.WEBSITE,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        allocated_budget=Decimal("100.00"),
        max_allowed_spend=Decimal("100.00"),
        success_criteria="s",
        failure_criteria="f",
    ))

    m1 = ExperimentMetrics(
        experiment_id=exp.id,
        impressions=500,
        clicks=25,
        visitors=20,
        conversions=2,
        conversion_rate=0.1,
        revenue=Decimal("180.00"),
        cost=Decimal("100.00"),
        profit_loss=Decimal("80.00"),
        roi=0.8,
    )
    saved_m1 = metrics_repo.create(m1)
    assert saved_m1.id == m1.id
    assert saved_m1.impressions == 500
    assert saved_m1.revenue == Decimal("180.00")

    # Retrieve by ID
    fetched_m1 = metrics_repo.get(m1.id)
    assert fetched_m1 is not None
    assert fetched_m1.id == m1.id
    assert fetched_m1.profit_loss == Decimal("80.00")

    # Record second snapshot
    m2 = ExperimentMetrics(
        experiment_id=exp.id,
        impressions=1200,
        clicks=60,
        revenue=Decimal("350.00"),
        cost=Decimal("150.00"),
        profit_loss=Decimal("200.00"),
    )
    metrics_repo.create(m2)

    history = metrics_repo.list_for_experiment(exp.id)
    assert len(history) == 2

    latest = metrics_repo.get_latest_for_experiment(exp.id)
    assert latest is not None
    assert latest.impressions == 1200


def test_metrics_foreign_key_enforced(session: Session):
    metrics_repo = MetricsRepository(session)
    m = ExperimentMetrics(
        experiment_id=uuid4(),  # Non-existent experiment ID
        impressions=100,
    )
    with pytest.raises(IntegrityError):
        metrics_repo.create(m)


# ── 5. Decision Repository Tests ─────────────────────────────────────────────

def test_create_and_retrieve_decision_for_opportunity(session: Session):
    opp_repo = OpportunityRepository(session)
    dec_repo = DecisionRepository(session)

    opp = opp_repo.create(Opportunity(title="Too competitive", description="d", category=OpportunityCategory.PRODUCT))
    dec = Decision(
        opportunity_id=opp.id,
        outcome=DecisionOutcome.KILL,
        reason="Market saturation too high for ₹1,000 budget.",
        evidence_summary="Found 15 established free alternatives.",
        confidence=0.9,
    )
    saved_dec = dec_repo.create(dec)
    assert saved_dec.id == dec.id
    assert saved_dec.opportunity_id == opp.id
    assert saved_dec.outcome == DecisionOutcome.KILL

    fetched = dec_repo.get(dec.id)
    assert fetched is not None
    assert fetched.opportunity_id == opp.id
    assert fetched.reason == dec.reason


def test_create_and_retrieve_decision_for_experiment(session: Session):
    opp_repo = OpportunityRepository(session)
    exp_repo = ExperimentRepository(session)
    dec_repo = DecisionRepository(session)

    opp = opp_repo.create(Opportunity(title="Opp", description="d", category=OpportunityCategory.PRODUCT))
    exp = exp_repo.create(Experiment(
        opportunity_id=opp.id,
        hypothesis="h",
        objective="o",
        channel=Channel.INSTAGRAM,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        allocated_budget=Decimal("100.00"),
        max_allowed_spend=Decimal("100.00"),
        success_criteria="s",
        failure_criteria="f",
    ))

    dec = Decision(
        experiment_id=exp.id,
        outcome=DecisionOutcome.SCALE,
        reason="ROAS reached 3.5x in first week.",
        confidence=0.85,
    )
    saved = dec_repo.create(dec)
    assert saved.experiment_id == exp.id
    assert saved.outcome == DecisionOutcome.SCALE

    decisions = dec_repo.list(experiment_id=exp.id)
    assert len(decisions) == 1
    assert decisions[0].id == dec.id


# ── 6. Isolation Tests ────────────────────────────────────────────────────────

def test_database_isolation(session: Session):
    """Verifies that each test session has an independent database."""
    repo = OpportunityRepository(session)
    opp = Opportunity(title="Isolation check", description="d", category=OpportunityCategory.PRODUCT)
    repo.create(opp)
    assert len(repo.list()) == 1

    # Clean second engine
    engine2 = get_engine("sqlite:///:memory:")
    init_db(engine2)
    session_factory2 = get_session_factory(engine2)
    with session_factory2() as session2:
        repo2 = OpportunityRepository(session2)
        assert len(repo2.list()) == 0
