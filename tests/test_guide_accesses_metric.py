"""Focused unit and integration tests for dedicated guide_accesses metric (Step 70).

Verifies:
1. `guide_accesses=None` remains valid on domain model, ORM, and repository.
2. A populated `guide_accesses` value survives Pydantic → ORM → repository → Pydantic round-trip.
3. Existing metrics without `guide_accesses` remain 100% compatible and functional.
4. `guide_accesses` participates in analysis/delta handling correctly (MetricDelta, observations).
5. Existing reporting-window and restatement behavior is unchanged (RESTATEMENT != NEW PERFORMANCE PERIOD).
6. Existing financial fields remain completely independent (zero ledger side-effects, zero financial mutations).
"""

from collections.abc import Generator
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.analysis.service import ExperimentAnalysisService
from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.measurement.service import ExperimentMeasurementService
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.metrics import ExperimentMetrics
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite database in memory for each test."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture
def test_experiment(session: Session) -> Experiment:
    """Creates a basic draft experiment in the database."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="Freelance Workflow Guide",
            description="Testing demand for freelance financial workflow guide.",
            category=OpportunityCategory.CONTENT,
            status=OpportunityStatus.DISCOVERED,
        )
    )

    exp_repo = ExperimentRepository(session)
    return exp_repo.create(
        Experiment(
            opportunity_id=opp.id,
            hypothesis="Freelancers will access the static guide.",
            objective="Observe guide access count.",
            channel=Channel.FACEBOOK,
            monetization_method=MonetizationMethod.AFFILIATE,
            allocated_budget=Decimal("0.00"),
            max_allowed_spend=Decimal("200.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Guide accesses >= 10",
            failure_criteria="Guide accesses == 0",
            status=ExperimentStatus.DRAFT,
        )
    )


# ── 1. guide_accesses=None Remains Valid ─────────────────────────────────────

def test_guide_accesses_none_default_and_explicit(test_experiment: Experiment, session: Session):
    """ExperimentMetrics defaults guide_accesses to None and accepts explicit None."""
    # Default is None
    m_default = ExperimentMetrics(experiment_id=test_experiment.id)
    assert m_default.guide_accesses is None

    # Explicit None
    m_explicit = ExperimentMetrics(experiment_id=test_experiment.id, guide_accesses=None)
    assert m_explicit.guide_accesses is None

    # Persist and retrieve via ExperimentMeasurementService
    recorded = ExperimentMeasurementService.record_measurement(session, m_default)
    assert recorded.guide_accesses is None

    retrieved = ExperimentMeasurementService.get_measurement(session, recorded.id)
    assert retrieved is not None
    assert retrieved.guide_accesses is None


# ── 2. Populated guide_accesses Round-Trip ───────────────────────────────────

def test_guide_accesses_round_trip(test_experiment: Experiment, session: Session):
    """Populated guide_accesses survives Pydantic → ORM → Repository → Pydantic round-trip."""
    metrics = ExperimentMetrics(
        experiment_id=test_experiment.id,
        evidence_type=EvidenceCategory.FACT,
        source_reference="edge:telemetry:guide_access:test:2026-10-01:2026-10-01",
        guide_accesses=42,
        cost=Decimal("0.00"),
    )

    # Persist via MetricsRepository directly
    repo = MetricsRepository(session, auto_commit=True)
    persisted = repo.create(metrics)
    assert persisted.guide_accesses == 42

    # Retrieve by ID
    retrieved = repo.get(metrics.id)
    assert retrieved is not None
    assert retrieved.guide_accesses == 42
    assert retrieved.evidence_type == EvidenceCategory.FACT
    assert retrieved.source_reference == "edge:telemetry:guide_access:test:2026-10-01:2026-10-01"

    # Retrieve latest for experiment
    latest = repo.get_latest_for_experiment(test_experiment.id)
    assert latest is not None
    assert latest.guide_accesses == 42

    # Retrieve list for experiment
    all_metrics = repo.list_for_experiment(test_experiment.id)
    assert len(all_metrics) == 1
    assert all_metrics[0].guide_accesses == 42


# ── 3. Existing Metrics Without guide_accesses Remain Compatible ─────────────

def test_existing_metrics_without_guide_accesses_compatibility(
    test_experiment: Experiment, session: Session
):
    """Measurements with standard funnel fields (impressions, clicks, visitors, conversions) work unchanged."""
    m = ExperimentMetrics(
        experiment_id=test_experiment.id,
        evidence_type=EvidenceCategory.FACT,
        source_reference="meta:insights:campaign:123:2026-10-01:2026-10-01",
        impressions=1000,
        clicks=50,
        visitors=40,
        conversions=4,
        cost=Decimal("25.00"),
        revenue=Decimal("100.00"),
    )

    recorded = ExperimentMeasurementService.record_measurement(session, m)
    assert recorded.guide_accesses is None
    assert recorded.impressions == 1000
    assert recorded.clicks == 50
    assert recorded.visitors == 40
    assert recorded.conversions == 4
    # conversion_rate uses visitors as base_count (4 / 40 = 0.10)
    assert recorded.conversion_rate == 0.10
    assert recorded.profit_loss == Decimal("75.00")

    analysis = ExperimentAnalysisService.analyze(session, test_experiment.id)
    assert "visitors" in analysis.available_metrics
    assert "guide_accesses" in analysis.missing_metrics


# ── 4. guide_accesses Participates in Analysis & Deltas Correctly ─────────────

def test_guide_accesses_in_analysis_and_deltas(test_experiment: Experiment, session: Session):
    """guide_accesses appears in available_metrics, observations, and computes MetricDelta."""
    # Checkpoint 1
    m1 = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=test_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="edge:telemetry:guide_access:test:2026-10-01:2026-10-01",
            guide_accesses=10,
            cost=Decimal("0.00"),
            recorded_at=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        ),
    )

    # Checkpoint 2
    m2 = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=test_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="edge:telemetry:guide_access:test:2026-10-02:2026-10-02",
            guide_accesses=25,
            cost=Decimal("0.00"),
            recorded_at=datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, test_experiment.id)
    assert analysis.total_measurements == 2
    assert "guide_accesses" in analysis.available_metrics

    # Observation statement check
    obs_statements = [o.statement for o in analysis.observations]
    assert any("Latest recorded guide accesses count is 25." in s for s in obs_statements)

    # MetricDelta check
    delta_metrics = {d.metric_name: d for d in analysis.changes_from_previous}
    assert "guide_accesses" in delta_metrics

    delta = delta_metrics["guide_accesses"]
    assert delta.previous_value == 10
    assert delta.latest_value == 25
    assert delta.absolute_change == 15
    assert delta.percentage_change == 150.0
    assert delta.statement == "Guide Accesses changed from 10 to 25 (delta: +15)."


# ── 5. Reporting-Window / Restatement Invariant Preserved ─────────────────────

def test_guide_accesses_restatement_preserves_single_checkpoint(
    test_experiment: Experiment, session: Session
):
    """Restatement with updated guide_accesses does not create a new performance period."""
    same_window = "edge:telemetry:guide_access:test:2026-10-01:2026-10-01"

    # Initial observation for Day 1
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=test_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference=same_window,
            guide_accesses=10,
            cost=Decimal("0.00"),
            recorded_at=datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc),
        ),
    )

    # Restatement for Day 1 (late edge requests arrive)
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=test_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference=same_window,
            guide_accesses=14,
            cost=Decimal("0.00"),
            recorded_at=datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc),
        ),
    )

    analysis = ExperimentAnalysisService.analyze(session, test_experiment.id)
    # 2 measurement records persisted
    assert analysis.total_measurements == 2

    # Resolved to a single checkpoint (RESTATEMENT != NEW PERFORMANCE PERIOD)
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.guide_accesses == 14

    # No delta between checkpoints because only 1 unique logical window exists
    assert analysis.changes_from_previous == []

    obs_statements = [o.statement for o in analysis.observations]
    assert any("Latest recorded guide accesses count is 14." in s for s in obs_statements)


# ── 6. Financial Independence / Zero Side-Effects ────────────────────────────

def test_guide_accesses_financial_independence(test_experiment: Experiment, session: Session):
    """Recording guide_accesses creates zero financial side-effects or ledger changes."""
    cap_repo = CapitalRepository(session)
    balance_before = cap_repo.get_current_balance()
    txs_before = cap_repo.get_transaction_history()

    # Record measurement with high guide_accesses
    m = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=test_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="edge:telemetry:guide_access:test:2026-10-01:2026-10-01",
            guide_accesses=500,
            cost=Decimal("0.00"),
            revenue=None,
            profit_loss=None,
            roi=None,
            roas=None,
        ),
    )

    # Invariant checks:
    # 1. Measurement financial fields are unmutated
    assert m.guide_accesses == 500
    assert m.cost == Decimal("0.00")
    assert m.revenue is None
    assert m.profit_loss is None
    assert m.roi is None
    assert m.roas is None

    # 2. Capital ledger has zero new transactions
    balance_after = cap_repo.get_current_balance()
    txs_after = cap_repo.get_transaction_history()
    assert balance_after == balance_before
    assert len(txs_after) == len(txs_before)

    # 3. Experiment status and actual spend are untouched
    exp_repo = ExperimentRepository(session)
    exp = exp_repo.get(test_experiment.id)
    assert exp is not None
    assert exp.actual_spend == Decimal("0.00")
    assert exp.status == ExperimentStatus.DRAFT
