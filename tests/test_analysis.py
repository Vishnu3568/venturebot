"""Unit and integration tests for Experiment Performance Analysis Foundation (Step 12).

Verifies:
1. Experiment with zero measurements returns clean analysis with explicit missing metrics.
2. Experiment with one measurement returns factual performance summary.
3. Experiment with multiple measurements computes exact metric deltas over time.
4. Missing values are explicitly identified and NOT converted into fake zeros or estimates.
5. Evidence taxonomy is preserved: FACT for direct records, INFERENCE for computed deltas.
6. Non-existent experiment reference raises ValueError.
7. Zero financial side-effects (zero capital transactions, zero balance/spend mutations).
8. Zero decision side-effects (zero decisions created, no automated SCALE/ITERATE/KILL/HOLD).
9. All Step 1-11 invariants continue passing.
"""

from collections.abc import Generator
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.analysis.service import ExperimentAnalysisService
from venturebot.approval.service import ExperimentApprovalService
from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.execution.service import ExperimentExecutionService
from venturebot.measurement.service import ExperimentMeasurementService
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.metrics import ExperimentMetrics
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite in-memory database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture
def running_experiment(session: Session) -> Experiment:
    """Sets up an opportunity, funds capital ledger, approves and starts an experiment."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="Resume Reviewer Plugin",
            description="Browser tool providing ATS optimization for developers.",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="Developer Forum",
            evidence_notes="Verified interest from 10 applicants",
            audience="Software engineers",
            trend_strength="High",
            growth_indicators="Query volume up 25%",
            competition_level="Low",
            monetization_notes="₹299 one-time license",
            production_difficulty="Low",
            distribution_difficulty="Direct outreach",
            automation_potential="High",
            platform_dependency="LinkedIn",
            regulatory_notes="None",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("1200.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
            confidence=0.8,
        )
    )

    exp_repo = ExperimentRepository(session)
    exp = exp_repo.create(
        Experiment(
            opportunity_id=opp.id,
            hypothesis="Direct LinkedIn outreach will yield at least 3 paid licenses.",
            objective="Validate willingness to pay for ATS resume formatter.",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="At least 3 paying users (₹897 revenue) with spend <= ₹50.",
            failure_criteria="Zero paying users from 30 outreach messages.",
            status=ExperimentStatus.DRAFT,
        )
    )

    ExperimentApprovalService.approve(session, exp.id, reason="Approved pilot")
    start_res = ExperimentExecutionService.start(session, exp.id)
    assert start_res.is_successful is True
    assert start_res.experiment is not None
    return start_res.experiment


# ── 1. Zero Measurements ──────────────────────────────────────────────────────

def test_analysis_with_no_measurements(session: Session, running_experiment: Experiment):
    """An experiment with no recorded measurements yields clean zero-state analysis."""
    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    assert analysis.experiment_id == running_experiment.id
    assert analysis.opportunity_id == running_experiment.opportunity_id
    assert analysis.total_measurements == 0
    assert analysis.latest_measurement is None
    assert analysis.available_metrics == []
    assert len(analysis.missing_metrics) > 0
    assert "revenue" in analysis.missing_metrics
    assert "conversions" in analysis.missing_metrics

    # Financial summary values are None, NOT fake zeros
    assert analysis.financial_summary["revenue"] is None
    assert analysis.financial_summary["cost"] is None
    assert analysis.financial_summary["profit_loss"] is None
    assert analysis.financial_summary["roi"] is None
    assert analysis.financial_summary["roas"] is None

    assert analysis.changes_from_previous == []
    assert len(analysis.observations) == 1
    assert analysis.observations[0].category == EvidenceCategory.FACT
    assert "No measurements have been recorded" in analysis.observations[0].statement


# ── 2. Single Measurement ─────────────────────────────────────────────────────

def test_analysis_with_single_measurement(session: Session, running_experiment: Experiment):
    """An experiment with one measurement extracts factual summary without deltas."""
    measurement = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Stripe charges #101-#102",
            visitors=30,
            conversions=2,
            revenue=Decimal("598.00"),
            cost=Decimal("30.00"),
            retention_notes="2 users active after 48h",
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    assert analysis.total_measurements == 1
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.id == measurement.id

    # Available metrics
    assert "revenue" in analysis.available_metrics
    assert "cost" in analysis.available_metrics
    assert "visitors" in analysis.available_metrics
    assert "conversions" in analysis.available_metrics
    assert "conversion_rate" in analysis.available_metrics
    assert "profit_loss" in analysis.available_metrics
    assert "retention_notes" in analysis.available_metrics

    # Missing metrics explicitly recorded
    assert "impressions" in analysis.missing_metrics
    assert "clicks" in analysis.missing_metrics

    # Financial summary
    assert analysis.financial_summary["revenue"] == Decimal("598.00")
    assert analysis.financial_summary["cost"] == Decimal("30.00")
    assert analysis.financial_summary["profit_loss"] == Decimal("568.00")
    assert analysis.financial_summary["roi"] == pytest.approx(18.93, rel=1e-2)
    assert analysis.financial_summary["roas"] == Decimal("19.93")

    # No changes from previous because only 1 measurement exists
    assert analysis.changes_from_previous == []

    # Factual observations
    obs_statements = [o.statement for o in analysis.observations]
    assert any("₹598.00" in s for s in obs_statements)
    assert any("₹30.00" in s for s in obs_statements)
    assert any("₹568.00" in s for s in obs_statements)
    assert any("Missing/unmeasured" in s for s in obs_statements)


# ── 3. Multiple Measurements & Deltas ──────────────────────────────────────────

def test_analysis_with_multiple_measurements_computes_deltas(
    session: Session, running_experiment: Experiment
):
    """With multiple measurements, factual changes between consecutive snapshots are computed."""
    # Snapshot 1
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Day 1 audit",
            visitors=20,
            conversions=1,
            revenue=Decimal("299.00"),
            cost=Decimal("20.00"),
            recorded_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        ),
    )

    # Snapshot 2
    m2 = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Day 3 audit",
            visitors=50,
            conversions=4,
            revenue=Decimal("1196.00"),
            cost=Decimal("45.00"),
            recorded_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    assert analysis.total_measurements == 2
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.id == m2.id

    # Check deltas
    delta_map = {d.metric_name: d for d in analysis.changes_from_previous}
    assert "revenue" in delta_map
    assert delta_map["revenue"].previous_value == Decimal("299.00")
    assert delta_map["revenue"].latest_value == Decimal("1196.00")
    assert delta_map["revenue"].absolute_change == Decimal("897.00")
    assert delta_map["revenue"].percentage_change == pytest.approx(300.0, rel=1e-1)

    assert "cost" in delta_map
    assert delta_map["cost"].previous_value == Decimal("20.00")
    assert delta_map["cost"].latest_value == Decimal("45.00")
    assert delta_map["cost"].absolute_change == Decimal("25.00")

    assert "visitors" in delta_map
    assert delta_map["visitors"].absolute_change == 30

    assert "conversions" in delta_map
    assert delta_map["conversions"].absolute_change == 3

    # Inferences exist for the deltas
    inference_obs = [o for o in analysis.observations if o.category == EvidenceCategory.INFERENCE]
    assert len(inference_obs) > 0
    assert any("Revenue" in o.statement for o in inference_obs)


# ── 4. Error Handling & Guardrails ────────────────────────────────────────────

def test_analysis_rejects_non_existent_experiment(session: Session):
    """Attempting analysis on a non-existent experiment raises ValueError."""
    fake_id = uuid4()
    with pytest.raises(ValueError, match="does not exist"):
        ExperimentAnalysisService.analyze(session, fake_id)


def test_analysis_has_zero_financial_side_effects(
    session: Session, running_experiment: Experiment
):
    """
    Financial safety invariant:
    Running analysis must NEVER:
    1. Create capital transactions.
    2. Change liquid capital balance.
    3. Change ledger actual spend.
    4. Mutate allocations.
    """
    cap_repo = CapitalRepository(session)
    summary_before = cap_repo.get_financial_summary()
    tx_count_before = len(cap_repo.get_transaction_history())

    ExperimentAnalysisService.analyze(session, running_experiment.id)

    summary_after = cap_repo.get_financial_summary()
    tx_count_after = len(cap_repo.get_transaction_history())

    assert tx_count_after == tx_count_before
    assert summary_after.current_balance == summary_before.current_balance
    assert summary_after.total_cost == summary_before.total_cost
    assert summary_after.total_allocated == summary_before.total_allocated
    assert summary_after.available_unallocated == summary_before.available_unallocated


def test_analysis_does_not_create_decisions(
    session: Session, running_experiment: Experiment
):
    """
    Step 12 Analysis layer must NOT create decisions (no auto SCALE/KILL/ITERATE/HOLD).
    Decision count must remain strictly unchanged.
    """
    # Record outstanding results
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("100000.00"),
            cost=Decimal("50.00"),
            conversions=100,
        ),
    )

    dec_repo = DecisionRepository(session)
    decisions_before = dec_repo.list(experiment_id=running_experiment.id)

    # Perform analysis
    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)
    assert analysis.total_measurements == 1

    decisions_after = dec_repo.list(experiment_id=running_experiment.id)
    assert len(decisions_after) == len(decisions_before)


def test_analysis_with_nullable_revenue_handles_unknown_gracefully(
    session: Session, running_experiment: Experiment
):
    """Experiment with unknown revenue (revenue=None) produces clean analysis without crashing."""
    measurement = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Meta campaign insights",
            impressions=1200,
            clicks=35,
            revenue=None,
            cost=Decimal("45.50"),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    assert analysis.total_measurements == 1
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.id == measurement.id

    # revenue and profit_loss are treated as missing
    assert "revenue" in analysis.missing_metrics
    assert "profit_loss" in analysis.missing_metrics
    assert "cost" in analysis.available_metrics

    # financial summary preserves None
    assert analysis.financial_summary["revenue"] is None
    assert analysis.financial_summary["profit_loss"] is None
    assert analysis.financial_summary["cost"] == Decimal("45.50")
    assert analysis.financial_summary["roi"] is None
    assert analysis.financial_summary["roas"] is None

    # Factual observations do NOT format currency statements for None revenue/profit_loss
    obs_statements = [o.statement for o in analysis.observations]
    assert any("Latest recorded cost is ₹45.50." in s for s in obs_statements)
    assert not any("Latest recorded revenue" in s for s in obs_statements)
    assert not any("Latest recorded profit/loss" in s for s in obs_statements)
    assert any("Missing/unmeasured" in s for s in obs_statements)


# ── 5. Step 46 Reporting-Window Checkpoint Resolution ─────────────────────────

def test_same_reporting_window_restatement_produces_no_sequential_delta(
    session: Session, running_experiment: Experiment
):
    """Same reporting window original + restatement: only latest observation becomes checkpoint, zero deltas."""
    # Window W1 original (spend 50, clicks 20)
    m_orig = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="meta:insights:campaign:12345:2026-09-01:2026-09-02",
            cost=Decimal("50.00"),
            impressions=1000,
            clicks=20,
            recorded_at=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc),
        ),
    )

    # Window W1 restatement (spend 55, clicks 22, later recorded_at)
    m_restated = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="meta:insights:campaign:12345:2026-09-01:2026-09-02",
            cost=Decimal("55.00"),
            impressions=1000,
            clicks=22,
            recorded_at=datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    # Total measurements counts raw rows in DB
    assert analysis.total_measurements == 2
    # Resolved checkpoint is latest observation (the restatement)
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.id == m_restated.id
    assert analysis.financial_summary["cost"] == Decimal("55.00")

    # RESTATEMENT != NEW PERFORMANCE PERIOD: no sequential delta fabricated
    assert analysis.changes_from_previous == []
    inference_obs = [o for o in analysis.observations if o.category == EvidenceCategory.INFERENCE]
    assert len(inference_obs) == 0


def test_restated_window_with_subsequent_window_computes_correct_delta(
    session: Session, running_experiment: Experiment
):
    """Sequential delta compares latest(W1) -> W2, not W1(orig) -> W1(restatement)."""
    # W1 original (cost 50, clicks 20)
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="meta:insights:campaign:12345:2026-09-01:2026-09-02",
            cost=Decimal("50.00"),
            clicks=20,
            recorded_at=datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
        ),
    )

    # W1 restatement (cost 55, clicks 25)
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="meta:insights:campaign:12345:2026-09-01:2026-09-02",
            cost=Decimal("55.00"),
            clicks=25,
            recorded_at=datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc),
        ),
    )

    # W2 (cost 70, clicks 35)
    m_w2 = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="meta:insights:campaign:12345:2026-09-03:2026-09-04",
            cost=Decimal("70.00"),
            clicks=35,
            recorded_at=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    assert analysis.total_measurements == 3
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.id == m_w2.id
    assert analysis.financial_summary["cost"] == Decimal("70.00")

    delta_map = {d.metric_name: d for d in analysis.changes_from_previous}
    assert "cost" in delta_map
    # Compares latest W1 (55) -> W2 (70), delta = +15 (NOT 50 -> 55, delta = +5)
    assert delta_map["cost"].previous_value == Decimal("55.00")
    assert delta_map["cost"].latest_value == Decimal("70.00")
    assert delta_map["cost"].absolute_change == Decimal("15.00")

    assert "clicks" in delta_map
    assert delta_map["clicks"].previous_value == 25
    assert delta_map["clicks"].latest_value == 35
    assert delta_map["clicks"].absolute_change == 10


def test_conceptual_scenario_with_named_windows(
    session: Session, running_experiment: Experiment
):
    """Section 6 conceptual scenario: W1(50, T1), W1(55, T2), W2(70, T3) -> delta = +15."""
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="W1",
            cost=Decimal("50.00"),
            recorded_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        ),
    )
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="W1",
            cost=Decimal("55.00"),
            recorded_at=datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
        ),
    )
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="W2",
            cost=Decimal("70.00"),
            recorded_at=datetime(2026, 9, 3, 10, 0, tzinfo=timezone.utc),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    assert analysis.total_measurements == 3
    delta_map = {d.metric_name: d for d in analysis.changes_from_previous}
    assert delta_map["cost"].previous_value == Decimal("55.00")
    assert delta_map["cost"].latest_value == Decimal("70.00")
    assert delta_map["cost"].absolute_change == Decimal("15.00")


def test_later_arriving_restatement_preserves_chronological_checkpoint_ordering(
    session: Session, running_experiment: Experiment
):
    """When a restatement for W1 is recorded at T3 (after W2 was recorded at T2),
    W1 is still ordered before W2 in checkpoints, and latest(W1) -> W2 is compared."""
    # W1 original (T1)
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="meta:insights:campaign:12345:2026-09-01:2026-09-02",
            cost=Decimal("50.00"),
            recorded_at=datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
        ),
    )
    # W2 (T2)
    m_w2 = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="meta:insights:campaign:12345:2026-09-03:2026-09-04",
            cost=Decimal("70.00"),
            recorded_at=datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc),
        ),
    )
    # W1 restatement arrives at T3 > T2
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="meta:insights:campaign:12345:2026-09-01:2026-09-02",
            cost=Decimal("55.00"),
            recorded_at=datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    assert analysis.total_measurements == 3
    # Latest chronological performance checkpoint is W2
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.id == m_w2.id
    assert analysis.financial_summary["cost"] == Decimal("70.00")

    delta_map = {d.metric_name: d for d in analysis.changes_from_previous}
    assert delta_map["cost"].previous_value == Decimal("55.00")
    assert delta_map["cost"].latest_value == Decimal("70.00")
    assert delta_map["cost"].absolute_change == Decimal("15.00")


def test_unwindowed_observations_without_source_reference_retain_sequential_deltas(
    session: Session, running_experiment: Experiment
):
    """Measurements with empty source_reference each remain distinct checkpoints."""
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="",
            cost=Decimal("10.00"),
            recorded_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        ),
    )
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="",
            cost=Decimal("25.00"),
            recorded_at=datetime(2026, 9, 2, 10, 0, tzinfo=timezone.utc),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, running_experiment.id)

    assert analysis.total_measurements == 2
    delta_map = {d.metric_name: d for d in analysis.changes_from_previous}
    assert delta_map["cost"].previous_value == Decimal("10.00")
    assert delta_map["cost"].latest_value == Decimal("25.00")
    assert delta_map["cost"].absolute_change == Decimal("15.00")

