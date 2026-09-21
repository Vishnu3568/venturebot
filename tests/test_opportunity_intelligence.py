"""Tests for Step 14 — Opportunity Intelligence Synthesis Foundation.

Verifies:
1. Opportunity with no experiments.
2. Opportunity with one experiment.
3. Opportunity with multiple experiments.
4. Experiment summaries contain only supported fields.
5. Actual spend comes from the authoritative ledger.
6. Allocated budget is not treated as actual spend.
7. Measured revenue aggregation uses recorded metrics only.
8. Missing revenue is not fabricated (when no measurements exist, returns None).
9. Profit/loss aggregation follows existing canonical metric semantics.
10. Linked learning records are returned.
11. No learning is automatically generated.
12. Opportunity evaluation is included.
13. Nonexistent opportunity is rejected using existing conventions.
14. Calling intelligence synthesis creates zero capital transactions.
15. Capital balance is unchanged.
16. No decisions are created.
17. No experiment/opportunity status changes.
18. Service is strictly read-only.
"""

from collections.abc import Generator
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.evaluation.models import EvaluationStatus
from venturebot.execution.service import ExperimentExecutionService
from venturebot.learning.models import ExperimentLearning
from venturebot.learning.service import ExperimentLearningService
from venturebot.measurement.service import ExperimentMeasurementService
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.metrics import ExperimentMetrics
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus
from venturebot.opportunity.models import OpportunityExperimentSummary, OpportunityIntelligenceContext
from venturebot.opportunity.service import OpportunityIntelligenceService


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite in-memory database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture
def sample_opportunity(session: Session) -> Opportunity:
    """Persisted opportunity fixture."""
    repo = OpportunityRepository(session)
    return repo.create(
        Opportunity(
            title="AI Resume Optimizer",
            description="Tool to optimize resumes for ATS and recruiter screening.",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.APPROVED,
            source="Engineering forum survey",
            evidence_notes="Verified 20 developers with ATS drop-off issues",
            audience="Job seekers in tech",
            trend_strength="Rising",
            growth_indicators="Search queries up 40%",
            competition_level="Moderate",
            monetization_notes="₹299 one-time download fee",
            production_difficulty="Low",
            distribution_difficulty="Direct developer community outreach",
            automation_potential="High",
            platform_dependency="Web",
            regulatory_notes="None",
            estimated_revenue_min=Decimal("600.00"),
            estimated_revenue_max=Decimal("1500.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
            confidence=0.85,
        )
    )


# ── Tests 1 to 4: Experiments Structure & Absence ─────────────────────────────────

def test_opportunity_with_no_experiments(session: Session, sample_opportunity: Opportunity):
    """Test 1: Opportunity with zero experiments produces empty summaries and None for metrics."""
    intelligence = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)

    assert isinstance(intelligence, OpportunityIntelligenceContext)
    assert intelligence.opportunity.id == sample_opportunity.id
    assert intelligence.total_experiments == 0
    assert intelligence.experiments_summary == []
    assert intelligence.total_actual_spend == Decimal("0.00")
    assert intelligence.total_measured_revenue is None
    assert intelligence.net_measured_profit_loss is None
    assert intelligence.accumulated_learnings == []
    assert intelligence.evaluation.status == EvaluationStatus.READY_FOR_EXPERIMENT_DESIGN


def test_opportunity_with_one_experiment(session: Session, sample_opportunity: Opportunity):
    """Test 2: Opportunity with one experiment correctly aggregates experiment info."""
    exp_repo = ExperimentRepository(session)
    exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Offering ATS optimization for ₹299 will convert >= 5%",
            objective="Validate conversions",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="3 sales",
            failure_criteria="0 sales from 50 contacts",
            status=ExperimentStatus.RUNNING,
        )
    )

    intelligence = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intelligence.total_experiments == 1
    assert len(intelligence.experiments_summary) == 1

    summary = intelligence.experiments_summary[0]
    assert isinstance(summary, OpportunityExperimentSummary)
    assert summary.experiment_id == exp.id
    assert summary.channel == Channel.LINKEDIN
    assert summary.status == ExperimentStatus.RUNNING
    assert summary.actual_spend == Decimal("0.00")


def test_opportunity_with_multiple_experiments(session: Session, sample_opportunity: Opportunity):
    """Test 3 & 4: Multiple experiments are all represented with strictly supported fields."""
    exp_repo = ExperimentRepository(session)
    exp1 = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="LinkedIn outreach hypothesis",
            objective="Objective 1",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="3 sales",
            failure_criteria="0 sales",
            status=ExperimentStatus.COMPLETED,
        )
    )
    exp2 = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Twitter outreach hypothesis",
            objective="Objective 2",
            channel=Channel.TWITTER_X,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("30.00"),
            max_allowed_spend=Decimal("60.00"),
            success_criteria="2 sales",
            failure_criteria="0 sales",
            status=ExperimentStatus.RUNNING,
        )
    )

    intelligence = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intelligence.total_experiments == 2
    assert len(intelligence.experiments_summary) == 2

    # Check that summary contracts only hold supported fields
    exp_ids = {s.experiment_id for s in intelligence.experiments_summary}
    assert exp_ids == {exp1.id, exp2.id}

    for s in intelligence.experiments_summary:
        # Pydantic model dump contains strictly the 4 canonical fields
        fields = set(s.model_dump().keys())
        assert fields == {"experiment_id", "channel", "status", "actual_spend"}


# ── Tests 5 to 6: Spend vs Allocated Budget ──────────────────────────────────────

def test_actual_spend_comes_from_ledger_and_ignores_allocated_budget(
    session: Session, sample_opportunity: Opportunity
):
    """Test 5 & 6: Actual spend strictly reflects ledger EXPERIMENT_SPEND, not allocated_budget."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    exp_repo = ExperimentRepository(session)
    exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Direct outreach",
            objective="Sales",
            channel=Channel.EMAIL,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("150.00"),  # Allocated is ₹150
            max_allowed_spend=Decimal("200.00"),
            success_criteria="Sales",
            failure_criteria="No sales",
            status=ExperimentStatus.RUNNING,
        )
    )

    # Before recording spend, actual spend is ₹0.00 despite ₹150 budget
    intel_before = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intel_before.total_actual_spend == Decimal("0.00")
    assert intel_before.experiments_summary[0].actual_spend == Decimal("0.00")

    # Record controlled spend of ₹42.50
    ExperimentExecutionService.record_spend(
        session,
        experiment_id=exp.id,
        amount=Decimal("42.50"),
        description="Outreach email tool",
    )

    intel_after = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    # Reflects actual spend (₹42.50), NOT allocated budget (₹150.00)
    assert intel_after.total_actual_spend == Decimal("42.50")
    assert intel_after.experiments_summary[0].actual_spend == Decimal("42.50")


# ── Tests 7 to 9: Measured Metrics & Profit/Loss Aggregation ──────────────────────

def test_measured_revenue_and_profit_loss_aggregation(
    session: Session, sample_opportunity: Opportunity
):
    """Test 7, 8, 9: Aggregates recorded metrics accurately; does not fabricate missing data."""
    exp_repo = ExperimentRepository(session)
    exp1 = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Channel 1",
            objective="Goal",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="Sales",
            failure_criteria="None",
            status=ExperimentStatus.RUNNING,
        )
    )
    exp2 = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Channel 2",
            objective="Goal",
            channel=Channel.WEBSITE,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="Sales",
            failure_criteria="None",
            status=ExperimentStatus.RUNNING,
        )
    )

    # Before measurements: revenue and profit/loss are None (not 0.00)
    intel_before = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intel_before.total_measured_revenue is None
    assert intel_before.net_measured_profit_loss is None

    # Record metrics on exp1: Revenue ₹598.00, Cost ₹30.00 -> Profit ₹568.00
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=exp1.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("598.00"),
            cost=Decimal("30.00"),
            profit_loss=Decimal("568.00"),
        ),
    )

    # Record metrics on exp2: Revenue ₹0.00, Cost ₹25.00 -> Profit -₹25.00
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=exp2.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("0.00"),
            cost=Decimal("25.00"),
            profit_loss=Decimal("-25.00"),
        ),
    )

    intel_after = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intel_after.total_measured_revenue == Decimal("598.00")  # ₹598 + ₹0
    assert intel_after.net_measured_profit_loss == Decimal("543.00")  # ₹568 + (-₹25)


def test_measured_revenue_aggregation_with_nullable_revenue(
    session: Session, sample_opportunity: Opportunity
):
    """Verifies that revenue=None is skipped, revenue=0.00 is counted, and None + 0 + 500 = 500.
    Also verifies that if all experiment measurements have revenue=None, total_measured_revenue is None.
    """
    exp_repo = ExperimentRepository(session)
    exp1 = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Exp 1",
            objective="Goal",
            channel=Channel.FACEBOOK,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="Sales",
            failure_criteria="None",
            status=ExperimentStatus.RUNNING,
        )
    )
    exp2 = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Exp 2",
            objective="Goal",
            channel=Channel.INSTAGRAM,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="Sales",
            failure_criteria="None",
            status=ExperimentStatus.RUNNING,
        )
    )
    exp3 = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Exp 3",
            objective="Goal",
            channel=Channel.WEBSITE,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="Sales",
            failure_criteria="None",
            status=ExperimentStatus.RUNNING,
        )
    )

    # 1. First record only an experiment with revenue=None
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=exp1.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=None,
            cost=Decimal("40.00"),
        ),
    )

    intel_only_none = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intel_only_none.total_measured_revenue is None
    assert intel_only_none.net_measured_profit_loss is None

    # 2. Record exp2 with observed zero revenue: revenue=Decimal("0.00")
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=exp2.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("0.00"),
            cost=Decimal("30.00"),
            profit_loss=Decimal("-30.00"),
        ),
    )

    intel_with_zero = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intel_with_zero.total_measured_revenue == Decimal("0.00")
    assert intel_with_zero.net_measured_profit_loss == Decimal("-30.00")

    # 3. Record exp3 with known revenue: revenue=Decimal("500.00")
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=exp3.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("500.00"),
            cost=Decimal("50.00"),
            profit_loss=Decimal("450.00"),
        ),
    )

    # Verification: None + 0.00 + 500.00 = 500.00
    intel_all = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intel_all.total_measured_revenue == Decimal("500.00")
    # Profit/Loss: None + (-30.00) + 450.00 = 420.00
    assert intel_all.net_measured_profit_loss == Decimal("420.00")


# ── Tests 10 to 12: Learning & Evaluation Preservation ───────────────────────────

def test_linked_learning_records_preserved_and_not_auto_generated(
    session: Session, sample_opportunity: Opportunity
):
    """Test 10 & 11: Retrospective learnings are returned; zero learnings are auto-generated."""
    exp_repo = ExperimentRepository(session)
    exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="H",
            objective="O",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="S",
            failure_criteria="F",
            status=ExperimentStatus.COMPLETED,
        )
    )

    # Explicitly record a learning
    learning = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=exp.id,
            opportunity_id=sample_opportunity.id,
            summary="Retrospective finding",
            what_worked=["Demo video teaser"],
            what_failed=["Generic text post"],
            key_learnings=["Visual proof converts 3x higher"],
        ),
    )

    intel = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert len(intel.accumulated_learnings) == 1
    assert intel.accumulated_learnings[0].id == learning.id
    assert intel.accumulated_learnings[0].summary == "Retrospective finding"
    assert intel.accumulated_learnings[0].key_learnings == ["Visual proof converts 3x higher"]


def test_opportunity_evaluation_is_included(session: Session, sample_opportunity: Opportunity):
    """Test 12: Opportunity evaluation is deterministically computed and included."""
    intel = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)

    eval_result = intel.evaluation
    assert eval_result.opportunity_id == sample_opportunity.id
    assert eval_result.status == EvaluationStatus.READY_FOR_EXPERIMENT_DESIGN
    assert eval_result.has_source_evidence is True
    assert eval_result.has_economic_estimates is True
    assert eval_result.exceeds_starting_capital_risk is False


# ── Tests 13 to 17: Rejections, Isolation, and Read-Only Guarantees ─────────────────

def test_reject_nonexistent_opportunity(session: Session):
    """Test 13: Nonexistent opportunity ID raises ValueError."""
    fake_id = uuid4()
    with pytest.raises(ValueError) as exc:
        OpportunityIntelligenceService.get_intelligence(session, fake_id)
    assert f"Opportunity '{fake_id}' does not exist" in str(exc.value)


def test_intelligence_synthesis_is_strictly_read_only_with_zero_side_effects(
    session: Session, sample_opportunity: Opportunity
):
    """Test 14, 15, 16, 17: Service creates zero transactions, decisions, or status changes."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    exp_repo = ExperimentRepository(session)
    exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="H",
            objective="O",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="S",
            failure_criteria="F",
            status=ExperimentStatus.RUNNING,
        )
    )

    dec_repo = DecisionRepository(session)

    # Capture state before calling get_intelligence
    summary_before = cap_repo.get_financial_summary()
    tx_count_before = len(cap_repo.get_transaction_history())
    dec_count_before = len(dec_repo.list())
    opp_status_before = sample_opportunity.status
    exp_status_before = exp.status

    # Execute intelligence synthesis
    intel = OpportunityIntelligenceService.get_intelligence(session, sample_opportunity.id)
    assert intel is not None

    # Verify state after calling get_intelligence
    summary_after = cap_repo.get_financial_summary()
    tx_count_after = len(cap_repo.get_transaction_history())
    dec_count_after = len(dec_repo.list())

    opp_refetched = OpportunityRepository(session).get(sample_opportunity.id)
    exp_refetched = exp_repo.get(exp.id)

    # Zero capital transactions created
    assert tx_count_after == tx_count_before
    # Capital balance unchanged
    assert summary_after.current_balance == summary_before.current_balance
    assert summary_after.total_cost == summary_before.total_cost
    assert summary_after.total_allocated == summary_before.total_allocated
    assert summary_after.available_unallocated == summary_before.available_unallocated
    # Zero decisions created
    assert dec_count_after == dec_count_before
    # Statuses unchanged
    assert opp_refetched is not None and opp_refetched.status == opp_status_before
    assert exp_refetched is not None and exp_refetched.status == exp_status_before
