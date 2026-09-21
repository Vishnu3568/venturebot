"""Unit and integration tests for Experiment Measurement & Result Recording Foundation (Step 9).

Verifies:
1. Valid metrics can be recorded for an existing experiment.
2. Measurements can be retrieved by ID.
3. Chronological listing of measurements for an experiment.
4. Latest measurement retrieval returns the most recent snapshot.
5. Orphan/non-existent experiment references are strictly rejected.
6. Invalid metric values (e.g. negative cost/revenue, invalid conversion rates) are rejected.
7. Evidence classifications (FACT, INFERENCE, HYPOTHESIS, PREDICTION) and source references are preserved.
8. Evidence distinctions are strictly maintained and never collapsed into facts.
9. Strict financial isolation:
   - Zero capital transactions created by recording metrics.
   - Zero change to liquid capital balance.
   - Zero change to ledger actual spend.
   - Revenue metrics do NOT create financial REVENUE transactions.
   - Cost metrics do NOT create financial EXPERIMENT_SPEND transactions.
10. No automatic decision-making (zero Decision records created: no auto SCALE/ITERATE/KILL/HOLD).
11. Deterministic calculation of contract-defined derived metrics (profit_loss, conversion_rate, roi, roas)
    without inventing arbitrary scoring formulas or thresholds.
12. Zero fabricated data or artificial results.
"""

from datetime import datetime, timezone
from collections.abc import Generator
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

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
    """Isolated SQLite database in memory for each test."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture
def running_experiment(session: Session) -> Experiment:
    """Creates an opportunity, funds capital ledger, approves and starts an experiment."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="SaaS Churn Alert Micro-Plugin",
            description="Browser extension notifying teams about impending churn risk.",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="Industry Newsletter",
            evidence_notes="Verified interest from 12 early respondents",
            audience="B2B SaaS customer success leads",
            trend_strength="Growing",
            growth_indicators="Churn management searches +30%",
            competition_level="Low",
            monetization_notes="₹499/mo direct license",
            production_difficulty="Low",
            distribution_difficulty="Direct B2B outreach",
            automation_potential="High",
            platform_dependency="Chrome Web Store",
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
            hypothesis="Direct LinkedIn outreach with a demo loom will yield at least 3 paid trials.",
            objective="Validate willingness to pay for churn alert plugin.",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("70.00"),
            max_allowed_spend=Decimal("100.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="At least 3 paying customers (₹1,497 revenue) with spend <= ₹70.",
            failure_criteria="Zero paying trials after 50 outreach messages.",
            status=ExperimentStatus.DRAFT,
        )
    )

    app_res = ExperimentApprovalService.approve(
        session, experiment_id=exp.id, reason="Approved for B2B pilot test"
    )
    assert app_res.is_approved is True

    start_res = ExperimentExecutionService.start(session, experiment_id=exp.id)
    assert start_res.is_successful is True
    assert start_res.experiment is not None
    return start_res.experiment


# ── 1. Recording & Retrieval ──────────────────────────────────────────────────

def test_record_and_retrieve_valid_measurement(session: Session, running_experiment: Experiment):
    """Persists a valid measured observation and retrieves it by ID."""
    measurement_input = ExperimentMetrics(
        experiment_id=running_experiment.id,
        evidence_type=EvidenceCategory.FACT,
        source_reference="Stripe payment webhook + Chrome Web Store analytics",
        impressions=250,
        clicks=40,
        visitors=35,
        conversions=3,
        revenue=Decimal("1497.00"),
        cost=Decimal("50.00"),
        retention_notes="All 3 users active on day 3",
    )

    saved = ExperimentMeasurementService.record_measurement(session, measurement_input)

    assert saved.id == measurement_input.id
    assert saved.experiment_id == running_experiment.id
    assert saved.evidence_type == EvidenceCategory.FACT
    assert saved.source_reference == "Stripe payment webhook + Chrome Web Store analytics"
    assert saved.impressions == 250
    assert saved.clicks == 40
    assert saved.visitors == 35
    assert saved.conversions == 3
    assert saved.revenue == Decimal("1497.00")
    assert saved.cost == Decimal("50.00")
    assert saved.profit_loss == Decimal("1447.00")  # Deterministic 1497 - 50
    assert saved.conversion_rate == pytest.approx(0.0857, rel=1e-3)  # 3 / 35
    assert saved.roi == pytest.approx(28.94, rel=1e-2)  # 1447 / 50
    assert saved.roas == Decimal("29.94")  # 1497 / 50

    # Retrieve by ID
    fetched = ExperimentMeasurementService.get_measurement(session, saved.id)
    assert fetched is not None
    assert fetched.id == saved.id
    assert fetched.revenue == Decimal("1497.00")


def test_list_and_get_latest_measurements(session: Session, running_experiment: Experiment):
    """Maintains chronological history and returns latest snapshot accurately."""
    m1 = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Day 1 log",
            visitors=10,
            conversions=0,
            revenue=Decimal("0.00"),
            cost=Decimal("20.00"),
        ),
    )

    m2 = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Day 2 log",
            visitors=25,
            conversions=2,
            revenue=Decimal("998.00"),
            cost=Decimal("40.00"),
        ),
    )

    all_measurements = ExperimentMeasurementService.list_measurements_for_experiment(
        session, running_experiment.id
    )
    assert len(all_measurements) == 2
    assert all_measurements[0].id == m1.id
    assert all_measurements[1].id == m2.id

    latest = ExperimentMeasurementService.get_latest_measurement(
        session, running_experiment.id
    )
    assert latest is not None
    assert latest.id == m2.id
    assert latest.conversions == 2
    assert latest.revenue == Decimal("998.00")


# ── 2. Association & Input Validation ─────────────────────────────────────────

def test_orphan_experiment_measurement_rejected(session: Session):
    """Measurements referencing non-existent experiments are strictly rejected."""
    fake_id = uuid4()
    orphan_metric = ExperimentMetrics(
        experiment_id=fake_id,
        evidence_type=EvidenceCategory.FACT,
        visitors=50,
        revenue=Decimal("0.00"),
    )

    with pytest.raises(ValueError, match="does not exist"):
        ExperimentMeasurementService.record_measurement(session, orphan_metric)


def test_invalid_metrics_data_rejected():
    """Negative revenues, negative costs, and out-of-bounds conversion rates are rejected."""
    valid_id = uuid4()

    # Negative revenue rejected
    with pytest.raises(ValidationError):
        ExperimentMetrics(
            experiment_id=valid_id,
            revenue=Decimal("-10.00"),
        )

    # Negative cost rejected
    with pytest.raises(ValidationError):
        ExperimentMetrics(
            experiment_id=valid_id,
            cost=Decimal("-5.00"),
        )

    # Conversion rate > 1.0 rejected
    with pytest.raises(ValidationError):
        ExperimentMetrics(
            experiment_id=valid_id,
            conversion_rate=1.5,
        )


# ── 3. Evidence Classification Preservation ───────────────────────────────────

def test_evidence_categories_preserved(session: Session, running_experiment: Experiment):
    """FACT, INFERENCE, HYPOTHESIS, and PREDICTION categories are preserved distinctly."""
    fact = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Verified payment gateway statement #1029",
            revenue=Decimal("499.00"),
            cost=Decimal("20.00"),
        ),
    )
    assert fact.evidence_type == EvidenceCategory.FACT
    assert fact.source_reference == "Verified payment gateway statement #1029"

    prediction = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.PREDICTION,
            source_reference="Model forecast based on first 5 signups",
            revenue=Decimal("2500.00"),
            cost=Decimal("100.00"),
        ),
    )
    assert prediction.evidence_type == EvidenceCategory.PREDICTION
    assert prediction.source_reference == "Model forecast based on first 5 signups"

    inference = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.INFERENCE,
            source_reference="Extrapolated from 50% response rate on B2B DM cohort",
            visitors=100,
            conversions=8,
        ),
    )
    assert inference.evidence_type == EvidenceCategory.INFERENCE

    hypothesis_metric = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.HYPOTHESIS,
            source_reference="Target benchmark defined in proposal",
            revenue=Decimal("1000.00"),
        ),
    )
    assert hypothesis_metric.evidence_type == EvidenceCategory.HYPOTHESIS

    # Verify they remain distinct on retrieval
    retrieved_fact = ExperimentMeasurementService.get_measurement(session, fact.id)
    retrieved_pred = ExperimentMeasurementService.get_measurement(session, prediction.id)
    assert retrieved_fact is not None
    assert retrieved_fact.evidence_type == EvidenceCategory.FACT
    assert retrieved_pred is not None
    assert retrieved_pred.evidence_type == EvidenceCategory.PREDICTION


# ── 4. Financial Isolation & Separation ───────────────────────────────────────

def test_measurement_recording_is_strictly_isolated_from_financial_ledger(
    session: Session, running_experiment: Experiment
):
    """
    Financial safety invariant:
    Recording metrics (even with high revenue or high cost) must NEVER:
    1. Create capital transactions.
    2. Mutate liquid capital balance.
    3. Modify ledger actual spend.
    4. Auto-generate REVENUE or EXPERIMENT_SPEND transactions.
    """
    cap_repo = CapitalRepository(session)
    summary_before = cap_repo.get_financial_summary()
    tx_count_before = len(cap_repo.get_transaction_history())

    # Record metric with ₹5,000 revenue and ₹200 cost
    measurement = ExperimentMetrics(
        experiment_id=running_experiment.id,
        evidence_type=EvidenceCategory.FACT,
        source_reference="Observed external sales ledger",
        revenue=Decimal("5000.00"),
        cost=Decimal("200.00"),
        conversions=10,
    )
    saved = ExperimentMeasurementService.record_measurement(session, measurement)
    assert saved.id is not None

    summary_after = cap_repo.get_financial_summary()
    tx_count_after = len(cap_repo.get_transaction_history())

    # 1. Zero capital transactions created
    assert tx_count_after == tx_count_before

    # 2. Capital balance strictly unchanged
    assert summary_after.current_balance == summary_before.current_balance
    assert summary_after.current_balance == Decimal("1000.00")

    # 3. Total revenue on financial ledger remains ₹0.00 (not mutated by metrics)
    assert summary_after.total_revenue == Decimal("0.00")

    # 4. Total cost / spend on financial ledger remains ₹0.00 (not mutated by metrics)
    assert summary_after.total_cost == Decimal("0.00")
    assert summary_after.total_experiment_spending == Decimal("0.00")

    # 5. Allocations strictly unchanged
    assert summary_after.total_allocated == summary_before.total_allocated
    assert summary_after.available_unallocated == summary_before.available_unallocated


# ── 5. No Automated Decision Engine ──────────────────────────────────────────

def test_measurement_does_not_create_decisions(
    session: Session, running_experiment: Experiment
):
    """
    Step 9 must NOT automatically create SCALE, ITERATE, KILL, or HOLD decisions.
    Decision count must remain unchanged.
    """
    dec_repo = DecisionRepository(session)
    decisions_before = dec_repo.list(experiment_id=running_experiment.id)
    dec_count_before = len(decisions_before)

    # Record high-revenue metric
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("10000.00"),
            cost=Decimal("50.00"),
            conversions=20,
        ),
    )

    # Record zero-revenue / failing metric
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_experiment.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("0.00"),
            cost=Decimal("100.00"),
            conversions=0,
        ),
    )

    decisions_after = dec_repo.list(experiment_id=running_experiment.id)
    dec_count_after = len(decisions_after)

    # Zero automatic decisions created by measurement engine
    assert dec_count_after == dec_count_before


# ── 6. Financial Semantics: Unknown vs Observed Zero Revenue ─────────────────

def test_measurement_unknown_revenue_semantics(
    session: Session, running_experiment: Experiment
):
    """Unknown revenue (revenue=None) yields profit_loss=None, roi=None, roas=None."""
    measurement = ExperimentMetrics(
        experiment_id=running_experiment.id,
        evidence_type=EvidenceCategory.FACT,
        source_reference="Meta Insights Telemetry",
        revenue=None,
        cost=Decimal("50.00"),
    )
    saved = ExperimentMeasurementService.record_measurement(session, measurement)
    assert saved.revenue is None
    assert saved.cost == Decimal("50.00")
    assert saved.profit_loss is None
    assert saved.roi is None
    assert saved.roas is None

    fetched = ExperimentMeasurementService.get_measurement(session, saved.id)
    assert fetched is not None
    assert fetched.revenue is None
    assert fetched.profit_loss is None
    assert fetched.roi is None
    assert fetched.roas is None


def test_measurement_observed_zero_revenue_semantics(
    session: Session, running_experiment: Experiment
):
    """Observed zero revenue (revenue=Decimal('0.00')) yields profit_loss=-cost, roi=-1.0, roas=None."""
    measurement = ExperimentMetrics(
        experiment_id=running_experiment.id,
        evidence_type=EvidenceCategory.FACT,
        source_reference="Verified sales checkout with 0 sales",
        revenue=Decimal("0.00"),
        cost=Decimal("50.00"),
    )
    saved = ExperimentMeasurementService.record_measurement(session, measurement)
    assert saved.revenue == Decimal("0.00")
    assert saved.cost == Decimal("50.00")
    assert saved.profit_loss == Decimal("-50.00")
    assert saved.roi == -1.0
    assert saved.roas is None

    fetched = ExperimentMeasurementService.get_measurement(session, saved.id)
    assert fetched is not None
    assert fetched.revenue == Decimal("0.00")
    assert fetched.profit_loss == Decimal("-50.00")
    assert fetched.roi == -1.0
    assert fetched.roas is None


def test_measurement_known_revenue_deterministic_derivation(
    session: Session, running_experiment: Experiment
):
    """Known commercial revenue calculates profit_loss, roi, and roas deterministically."""
    measurement = ExperimentMetrics(
        experiment_id=running_experiment.id,
        evidence_type=EvidenceCategory.FACT,
        source_reference="Stripe checkout ledger",
        revenue=Decimal("500.00"),
        cost=Decimal("50.00"),
    )
    saved = ExperimentMeasurementService.record_measurement(session, measurement)
    assert saved.revenue == Decimal("500.00")
    assert saved.cost == Decimal("50.00")
    assert saved.profit_loss == Decimal("450.00")
    assert saved.roi == 9.0  # 450 / 50
    assert saved.roas == Decimal("10.00")  # 500 / 50
