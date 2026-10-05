"""Unit and integration contract tests for Guide Access Telemetry Ingestion (Step 72).

Verifies the locked contract between edge telemetry retrieval and VentureBot:
1. Valid GUIDE_ACCESS retrieval maps to guide_accesses FACT.
2. visitors remains None.
3. conversions remains None.
4. revenue/profit/ROI/ROAS remain None without evidence.
5. Same source_reference with identical count is idempotent (DUPLICATE_NO_OP).
6. Same reporting window with changed authoritative count is treated as a restatement (RESTATEMENT_APPENDED).
7. Different reporting windows are not treated as restatements (distinct checkpoints).
8. Unknown experiment is rejected.
9. Unsupported event is rejected.
10. No financial ledger mutation occurs.
"""

from collections.abc import Generator
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from venturebot.analysis.service import ExperimentAnalysisService
from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.measurement.guide_telemetry import (
    GuideAccessTelemetryIngestionResult,
    GuideAccessTelemetryIngestionService,
    GuideAccessTelemetrySummary,
    build_canonical_guide_access_source_reference,
)
from venturebot.measurement.telemetry import TelemetryIngestionStatus
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite database in memory for each test with starting capital initialized."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        cap_repo = CapitalRepository(sess)
        cap_repo.initialize_starting_capital()
        yield sess


@pytest.fixture
def test_experiment(session: Session) -> Experiment:
    """Creates a basic draft experiment in the database."""
    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="Freelance Financial Guide",
            description="Informational workflow guide for solopreneurs.",
            category=OpportunityCategory.CONTENT,
            status=OpportunityStatus.DISCOVERED,
        )
    )

    exp_repo = ExperimentRepository(session)
    return exp_repo.create(
        Experiment(
            opportunity_id=opp.id,
            hypothesis="Freelancers will open the static guide.",
            objective="Observe guide access requests.",
            channel=Channel.FACEBOOK,
            monetization_method=MonetizationMethod.AFFILIATE,
            allocated_budget=Decimal("0.00"),
            max_allowed_spend=Decimal("200.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Verified server-observed guide_accesses exist.",
            failure_criteria="Zero guide_accesses observed during reporting window.",
            status=ExperimentStatus.DRAFT,
        )
    )


# ── 1. Contract Validation ────────────────────────────────────────────────────

def test_summary_contract_validation(test_experiment: Experiment):
    """GuideAccessTelemetrySummary validates required fields, types, and constraints."""
    # Valid summary with auto-generated canonical source_reference
    summary = GuideAccessTelemetrySummary(
        experiment_id=test_experiment.id,
        event_type="guide_access",
        count=25,
        date_start=date(2026, 10, 1),
        date_stop=date(2026, 10, 1),
    )
    assert summary.count == 25
    assert summary.evidence_type == EvidenceCategory.FACT
    expected_ref = build_canonical_guide_access_source_reference(
        test_experiment.id, date(2026, 10, 1), date(2026, 10, 1)
    )
    assert summary.source_reference == expected_ref

    # Reject unsupported event type
    with pytest.raises(ValidationError):
        GuideAccessTelemetrySummary(
            experiment_id=test_experiment.id,
            event_type="page_view",
            count=10,
            date_start=date(2026, 10, 1),
            date_stop=date(2026, 10, 1),
        )

    # Reject negative count
    negative_count = int("-1")
    with pytest.raises(ValidationError):
        GuideAccessTelemetrySummary(
            experiment_id=test_experiment.id,
            event_type="guide_access",
            count=negative_count,
            date_start=date(2026, 10, 1),
            date_stop=date(2026, 10, 1),
        )

    # Reject date_stop < date_start
    with pytest.raises(ValidationError):
        GuideAccessTelemetrySummary(
            experiment_id=test_experiment.id,
            event_type="guide_access",
            count=5,
            date_start=date(2026, 10, 5),
            date_stop=date(2026, 10, 1),
        )


# ── 2. Mapping to FACT & Field Separation ────────────────────────────────────

def test_valid_guide_access_mapping_semantics(test_experiment: Experiment, session: Session):
    """Valid GUIDE_ACCESS retrieval maps to guide_accesses FACT while keeping visitors/conversions None."""
    summary = GuideAccessTelemetrySummary(
        experiment_id=test_experiment.id,
        event_type="guide_access",
        count=37,
        date_start=date(2026, 10, 1),
        date_stop=date(2026, 10, 1),
    )

    result = GuideAccessTelemetryIngestionService.ingest_summary(session, summary)

    assert result.status == TelemetryIngestionStatus.INGESTED
    assert result.experiment_id == test_experiment.id
    assert result.date_start == date(2026, 10, 1)
    assert result.date_stop == date(2026, 10, 1)

    m = result.metrics
    # 1. guide_accesses is populated with authoritative count
    assert m.guide_accesses == 37
    # 2. evidence_type is FACT
    assert m.evidence_type == EvidenceCategory.FACT
    # 3. visitors remains strictly None
    assert m.visitors is None
    # 4. conversions remains strictly None
    assert m.conversions is None
    assert m.conversion_rate is None
    # 5. financial metrics remain None (cost is None because telemetry does not fabricate cost)
    assert m.revenue is None
    assert m.profit_loss is None
    assert m.roi is None
    assert m.roas is None
    assert m.cost is None


# ── 3. Idempotency (Same Window + Same Count) ─────────────────────────────────

def test_idempotent_duplicate_no_op(test_experiment: Experiment, session: Session):
    """Same reporting window with identical count results in DUPLICATE_NO_OP without duplicate writes."""
    summary = GuideAccessTelemetrySummary(
        experiment_id=test_experiment.id,
        event_type="guide_access",
        count=15,
        date_start=date(2026, 10, 1),
        date_stop=date(2026, 10, 1),
    )

    first_result = GuideAccessTelemetryIngestionService.ingest_summary(session, summary)
    assert first_result.status == TelemetryIngestionStatus.INGESTED

    # Ingest again with identical payload
    second_result = GuideAccessTelemetryIngestionService.ingest_summary(session, summary)
    assert second_result.status == TelemetryIngestionStatus.DUPLICATE_NO_OP
    assert second_result.metrics.id == first_result.metrics.id
    assert "idempotent no-op" in second_result.message

    # Analysis confirms only 1 measurement exists
    analysis = ExperimentAnalysisService.analyze(session, test_experiment.id)
    assert analysis.total_measurements == 1
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.guide_accesses == 15


# ── 4. Restatement (Same Window + Updated Count) ──────────────────────────────

def test_restatement_appends_immutable_record_preserving_single_checkpoint(
    test_experiment: Experiment, session: Session
):
    """Same reporting window with changed count appends RESTATEMENT_APPENDED and preserves single checkpoint."""
    summary_initial = GuideAccessTelemetrySummary(
        experiment_id=test_experiment.id,
        event_type="guide_access",
        count=15,
        date_start=date(2026, 10, 1),
        date_stop=date(2026, 10, 1),
    )
    first_result = GuideAccessTelemetryIngestionService.ingest_summary(session, summary_initial)
    assert first_result.status == TelemetryIngestionStatus.INGESTED
    assert first_result.metrics.guide_accesses == 15

    # Restatement arrives (e.g. late edge log sync updates count from 15 to 22)
    summary_restated = GuideAccessTelemetrySummary(
        experiment_id=test_experiment.id,
        event_type="guide_access",
        count=22,
        date_start=date(2026, 10, 1),
        date_stop=date(2026, 10, 1),
    )
    second_result = GuideAccessTelemetryIngestionService.ingest_summary(session, summary_restated)
    assert second_result.status == TelemetryIngestionStatus.RESTATEMENT_APPENDED
    assert second_result.metrics.id != first_result.metrics.id
    assert second_result.metrics.guide_accesses == 22

    # Invariant: RESTATEMENT != NEW PERFORMANCE PERIOD
    analysis = ExperimentAnalysisService.analyze(session, test_experiment.id)
    assert analysis.total_measurements == 2
    assert analysis.latest_measurement is not None
    assert analysis.latest_measurement.guide_accesses == 22
    # Because both observations share the same source_reference, there is only 1 resolved checkpoint
    assert analysis.changes_from_previous == []


# ── 5. Distinct Reporting Windows ─────────────────────────────────────────────

def test_distinct_reporting_windows_create_sequential_checkpoints(
    test_experiment: Experiment, session: Session
):
    """Different reporting windows are treated as distinct periods and compute deltas."""
    # Day 1
    summary_day1 = GuideAccessTelemetrySummary(
        experiment_id=test_experiment.id,
        event_type="guide_access",
        count=10,
        date_start=date(2026, 10, 1),
        date_stop=date(2026, 10, 1),
    )
    res1 = GuideAccessTelemetryIngestionService.ingest_summary(session, summary_day1)
    assert res1.status == TelemetryIngestionStatus.INGESTED

    # Day 2
    summary_day2 = GuideAccessTelemetrySummary(
        experiment_id=test_experiment.id,
        event_type="guide_access",
        count=25,
        date_start=date(2026, 10, 2),
        date_stop=date(2026, 10, 2),
    )
    res2 = GuideAccessTelemetryIngestionService.ingest_summary(session, summary_day2)
    assert res2.status == TelemetryIngestionStatus.INGESTED

    analysis = ExperimentAnalysisService.analyze(session, test_experiment.id)
    assert analysis.total_measurements == 2

    # Sequential checkpoints produce MetricDelta
    delta_metrics = {d.metric_name: d for d in analysis.changes_from_previous}
    assert "guide_accesses" in delta_metrics
    assert delta_metrics["guide_accesses"].previous_value == 10
    assert delta_metrics["guide_accesses"].latest_value == 25
    assert delta_metrics["guide_accesses"].absolute_change == 15


# ── 6. Error Cases: Unknown Experiment & Unsupported Event ───────────────────

def test_unknown_experiment_rejected(session: Session):
    """Ingesting telemetry for non-existent experiment is rejected with ValueError."""
    unknown_id = uuid4()
    summary = GuideAccessTelemetrySummary(
        experiment_id=unknown_id,
        event_type="guide_access",
        count=10,
        date_start=date(2026, 10, 1),
        date_stop=date(2026, 10, 1),
    )

    with pytest.raises(ValueError, match="does not exist"):
        GuideAccessTelemetryIngestionService.ingest_summary(session, summary)


def test_unsupported_event_rejected_by_service(test_experiment: Experiment, session: Session):
    """Directly passing unsupported event type is rejected."""
    # Construct a summary with valid values, then test validation
    with pytest.raises(ValidationError):
        GuideAccessTelemetrySummary(
            experiment_id=test_experiment.id,
            event_type="invalid_event",
            count=10,
            date_start=date(2026, 10, 1),
            date_stop=date(2026, 10, 1),
        )


# ── 7. Financial Ledger Isolation ─────────────────────────────────────────────

def test_financial_ledger_isolation(test_experiment: Experiment, session: Session):
    """Ingestion creates zero financial transactions, zero spend, and zero balance changes."""
    cap_repo = CapitalRepository(session)
    balance_before = cap_repo.get_current_balance()
    txs_before = cap_repo.get_transaction_history()

    summary = GuideAccessTelemetrySummary(
        experiment_id=test_experiment.id,
        event_type="guide_access",
        count=1000,
        date_start=date(2026, 10, 1),
        date_stop=date(2026, 10, 1),
    )
    result = GuideAccessTelemetryIngestionService.ingest_summary(session, summary)
    assert result.status == TelemetryIngestionStatus.INGESTED

    # Invariants:
    balance_after = cap_repo.get_current_balance()
    txs_after = cap_repo.get_transaction_history()
    assert balance_after == balance_before
    assert len(txs_after) == len(txs_before)

    # Experiment status is unchanged
    exp_repo = ExperimentRepository(session)
    exp = exp_repo.get(test_experiment.id)
    assert exp is not None
    assert exp.actual_spend == Decimal("0.00")
    assert exp.status == ExperimentStatus.DRAFT
