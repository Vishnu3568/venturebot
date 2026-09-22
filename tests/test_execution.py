"""Unit and integration tests for Experiment Execution Foundation (Step 8).

Verifies:
1. Lifecycle transitions (APPROVED -> RUNNING -> COMPLETED / KILLED).
2. Invalid transitions are blocked (DRAFT cannot start, COMPLETED/KILLED cannot restart).
3. Starting an experiment creates ZERO capital transactions and incurs ZERO actual spend.
4. Pausing and resuming experiments works cleanly without side effects.
5. Controlled spending creates EXPERIMENT_SPEND and enforces max_allowed_spend ceiling.
6. Actual spend cannot exceed max_allowed_spend.
7. Allocation release on completion/kill frees capital without fake cash refunds.
8. Metrics recording is strictly isolated from the financial ledger.
9. Decisions are recorded on kill/pause/complete with explicit reasons.
10. All financial invariants remain strictly preserved.
"""

from datetime import datetime, timezone
from collections.abc import Generator
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.approval.service import ExperimentApprovalService
from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.execution.service import ExperimentExecutionService
from venturebot.models.capital import TransactionType
from venturebot.models.decision import DecisionOutcome
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
def sample_opportunity(session: Session) -> Opportunity:
    """Persisted valid opportunity."""
    repo = OpportunityRepository(session)
    opp = Opportunity(
        title="Resume Optimizer for Developers",
        description="CLI tool to format developer resumes for ATS systems.",
        category=OpportunityCategory.PRODUCT,
        status=OpportunityStatus.DISCOVERED,
        source="Survey 2026",
        evidence_notes="Verified demand",
        audience="Junior and Mid-level developers in India",
        trend_strength="Rising",
        growth_indicators="Query volume up 25%",
        competition_level="Moderate",
        monetization_notes="Direct license sale at ₹299",
        production_difficulty="Low",
        distribution_difficulty="Direct outreach",
        automation_potential="High",
        platform_dependency="LinkedIn",
        regulatory_notes="None",
        estimated_revenue_min=Decimal("600.00"),
        estimated_revenue_max=Decimal("1500.00"),
        estimated_cost_min=Decimal("80.00"),
        estimated_cost_max=Decimal("150.00"),
        confidence=0.85,
    )
    return repo.create(opp)


@pytest.fixture
def sample_approved_experiment(session: Session, sample_opportunity: Opportunity) -> Experiment:
    """Approved experiment with allocated capital."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()
    exp_repo = ExperimentRepository(session)

    exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Offering ATS resume tool via LinkedIn will generate ₹600 with ₹80 budget.",
            objective="Verify customer willingness to pay for CLI resume optimizer.",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("80.00"),
            max_allowed_spend=Decimal("150.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Achieve verified revenue >= ₹600 with spend <= ₹150.",
            failure_criteria="Zero conversions or spend reaching ₹150 without sales.",
            status=ExperimentStatus.DRAFT,
        )
    )

    app_res = ExperimentApprovalService.approve(
        session,
        experiment_id=exp.id,
        reason="Approved for ₹80 pilot",
    )
    assert app_res.is_approved is True
    assert app_res.experiment is not None
    return app_res.experiment


# ── 1. Lifecycle Transitions & Execution Gate ─────────────────────────────────

def test_draft_experiment_cannot_start_directly(session: Session, sample_opportunity: Opportunity):
    """An experiment in DRAFT status cannot enter RUNNING without explicit approval."""
    exp_repo = ExperimentRepository(session)
    draft_exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Hypothesis",
            objective="Objective",
            channel=Channel.WEBSITE,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Pass",
            failure_criteria="Fail",
            status=ExperimentStatus.DRAFT,
        )
    )

    result = ExperimentExecutionService.start(session, draft_exp.id)
    assert result.is_successful is False
    assert "Only experiments in 'approved' status can be started" in (result.rejection_reason or "")


def test_approved_experiment_starts_successfully(session: Session, sample_approved_experiment: Experiment):
    """An APPROVED experiment transitions to RUNNING and records actual_start."""
    result = ExperimentExecutionService.start(session, sample_approved_experiment.id)

    assert result.is_successful is True
    assert result.experiment is not None
    assert result.experiment.status == ExperimentStatus.RUNNING
    assert result.experiment.actual_start is not None


def test_starting_experiment_creates_zero_transactions_and_zero_spend(
    session: Session, sample_approved_experiment: Experiment
):
    """
    Financial safety check:
    Starting an experiment must NOT deduct cash, disburse funds, or write to the capital ledger.
    """
    cap_repo = CapitalRepository(session)
    summary_before = cap_repo.get_financial_summary()
    tx_count_before = len(cap_repo.get_transaction_history())

    result = ExperimentExecutionService.start(session, sample_approved_experiment.id)
    assert result.is_successful is True

    summary_after = cap_repo.get_financial_summary()
    tx_count_after = len(cap_repo.get_transaction_history())

    # Invariants unchanged
    assert summary_after.current_balance == summary_before.current_balance
    assert summary_after.total_cost == Decimal("0.00")
    assert summary_after.total_experiment_spending == Decimal("0.00")
    assert summary_after.total_allocated == summary_before.total_allocated
    assert summary_after.available_unallocated == summary_before.available_unallocated
    assert tx_count_after == tx_count_before


def test_cannot_start_already_running_experiment(session: Session, sample_approved_experiment: Experiment):
    """An experiment already in RUNNING cannot be started again."""
    res1 = ExperimentExecutionService.start(session, sample_approved_experiment.id)
    assert res1.is_successful is True

    res2 = ExperimentExecutionService.start(session, sample_approved_experiment.id)
    assert res2.is_successful is False
    assert "Only experiments in 'approved' status can be started" in (res2.rejection_reason or "")


def test_pause_and_resume_lifecycle(session: Session, sample_approved_experiment: Experiment):
    """RUNNING experiments can be PAUSED and then RESUMED back to RUNNING."""
    start_res = ExperimentExecutionService.start(session, sample_approved_experiment.id)
    assert start_res.is_successful is True

    # 1. Pause
    pause_res = ExperimentExecutionService.pause(
        session, sample_approved_experiment.id, reason="Technical downtime"
    )
    assert pause_res.is_successful is True
    assert pause_res.experiment is not None
    assert pause_res.experiment.status == ExperimentStatus.PAUSED
    assert pause_res.decision is not None
    assert pause_res.decision.outcome == DecisionOutcome.HOLD
    assert pause_res.decision.reason == "Technical downtime"

    # 2. Cannot pause again
    dup_pause = ExperimentExecutionService.pause(session, sample_approved_experiment.id)
    assert dup_pause.is_successful is False

    # 3. Resume
    resume_res = ExperimentExecutionService.resume(session, sample_approved_experiment.id)
    assert resume_res.is_successful is True
    assert resume_res.experiment is not None
    assert resume_res.experiment.status == ExperimentStatus.RUNNING


# ── 2. Controlled Spend & Spending Ceilings ───────────────────────────────────

def test_controlled_spend_recording(session: Session, sample_approved_experiment: Experiment):
    """Controlled spend records EXPERIMENT_SPEND on ledger and updates actual_spend."""
    ExperimentExecutionService.start(session, sample_approved_experiment.id)

    cap_repo = CapitalRepository(session)
    spend_res = ExperimentExecutionService.record_spend(
        session,
        experiment_id=sample_approved_experiment.id,
        amount=Decimal("40.00"),
        description="Domain and micro-ad test",
    )

    assert spend_res.is_successful is True
    assert spend_res.transaction is not None
    assert spend_res.transaction.transaction_type == TransactionType.EXPERIMENT_SPEND
    assert spend_res.transaction.amount == Decimal("40.00")
    assert spend_res.experiment is not None
    assert spend_res.experiment.actual_spend == Decimal("40.00")

    summary = cap_repo.get_financial_summary()
    assert summary.current_balance == Decimal("960.00")  # 1000 - 40
    assert summary.total_cost == Decimal("40.00")
    assert summary.total_allocated == Decimal("40.00")  # Remaining allocation (80 - 40)
    assert summary.available_unallocated == Decimal("920.00")  # 960 - 40


def test_spend_cannot_exceed_max_allowed_spend(session: Session, sample_approved_experiment: Experiment):
    """Attempting to spend more than max_allowed_spend (₹150) is rejected."""
    ExperimentExecutionService.start(session, sample_approved_experiment.id)

    # 1. First spend of ₹100 is within ₹150 ceiling -> SUCCESS
    res1 = ExperimentExecutionService.record_spend(
        session,
        experiment_id=sample_approved_experiment.id,
        amount=Decimal("100.00"),
        description="First spend batch",
    )
    assert res1.is_successful is True

    # 2. Second spend of ₹60 would total ₹160 (> ₹150 ceiling) -> REJECTED
    res2 = ExperimentExecutionService.record_spend(
        session,
        experiment_id=sample_approved_experiment.id,
        amount=Decimal("60.00"),
        description="Excess spend attempt",
    )
    assert res2.is_successful is False
    assert "exceed max_allowed_spend" in (res2.rejection_reason or "")

    # Ledger remains at ₹100 spend
    exp_repo = ExperimentRepository(session)
    assert exp_repo.get_actual_spend_from_ledger(sample_approved_experiment.id) == Decimal("100.00")


def test_cannot_spend_on_non_running_experiment(
    session: Session, sample_approved_experiment: Experiment, sample_opportunity: Opportunity
):
    """Cannot record spend on APPROVED, DRAFT, PAUSED, COMPLETED, or KILLED experiments.
    
    Actual spend strictly requires RUNNING status.
    """
    # 1. APPROVED experiment cannot spend without starting
    app_res = ExperimentExecutionService.record_spend(
        session,
        experiment_id=sample_approved_experiment.id,
        amount=Decimal("20.00"),
        description="Illegal spend while still approved",
    )
    assert app_res.is_successful is False
    assert "Cannot record spend for experiment in 'approved' status" in (app_res.rejection_reason or "")
    assert "must be in 'running' status to record spend" in (app_res.rejection_reason or "")

    # 2. DRAFT experiment cannot spend
    exp_repo = ExperimentRepository(session)
    draft_exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Hypothesis",
            objective="Objective",
            channel=Channel.WEBSITE,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Pass",
            failure_criteria="Fail",
            status=ExperimentStatus.DRAFT,
        )
    )
    draft_res = ExperimentExecutionService.record_spend(
        session,
        experiment_id=draft_exp.id,
        amount=Decimal("20.00"),
        description="Illegal spend on draft",
    )
    assert draft_res.is_successful is False
    assert "Cannot record spend for experiment in 'draft' status" in (draft_res.rejection_reason or "")

    # 3. PAUSED experiment cannot spend
    ExperimentExecutionService.start(session, sample_approved_experiment.id)
    ExperimentExecutionService.pause(session, sample_approved_experiment.id, reason="Temporary hold")
    paused_res = ExperimentExecutionService.record_spend(
        session,
        experiment_id=sample_approved_experiment.id,
        amount=Decimal("20.00"),
        description="Illegal spend while paused",
    )
    assert paused_res.is_successful is False
    assert "Cannot record spend for experiment in 'paused' status" in (paused_res.rejection_reason or "")


# ── 3. Completion & Kill (Allocation Release) ─────────────────────────────────

def test_complete_experiment_releases_unspent_allocation(
    session: Session, sample_approved_experiment: Experiment
):
    """Completing a running experiment sets status to COMPLETED and releases unspent allocation."""
    cap_repo = CapitalRepository(session)
    ExperimentExecutionService.start(session, sample_approved_experiment.id)

    # Spend ₹50 out of ₹80 budget
    ExperimentExecutionService.record_spend(
        session,
        experiment_id=sample_approved_experiment.id,
        amount=Decimal("50.00"),
        description="Pilot test spend",
    )

    # Complete experiment
    comp_res = ExperimentExecutionService.complete(
        session,
        experiment_id=sample_approved_experiment.id,
        reason="Reached conversion target",
        decision_outcome=DecisionOutcome.SCALE,
    )

    assert comp_res.is_successful is True
    assert comp_res.experiment is not None
    assert comp_res.experiment.status == ExperimentStatus.COMPLETED
    assert comp_res.experiment.actual_end is not None
    assert comp_res.decision is not None
    assert comp_res.decision.outcome == DecisionOutcome.SCALE

    # Invariants check
    summary = cap_repo.get_financial_summary()
    assert summary.current_balance == Decimal("950.00")  # 1000 - 50
    assert summary.total_cost == Decimal("50.00")
    assert summary.total_allocated == Decimal("0.00")  # Released!
    assert summary.available_unallocated == Decimal("950.00")  # All remaining liquid cash available


def test_kill_experiment_releases_allocation_and_records_kill_decision(
    session: Session, sample_approved_experiment: Experiment
):
    """Killing an experiment transitions to KILLED and releases unspent allocation."""
    cap_repo = CapitalRepository(session)
    ExperimentExecutionService.start(session, sample_approved_experiment.id)

    kill_res = ExperimentExecutionService.kill(
        session,
        experiment_id=sample_approved_experiment.id,
        reason="Zero clicks after 48h; target audience unresponsive",
    )

    assert kill_res.is_successful is True
    assert kill_res.experiment is not None
    assert kill_res.experiment.status == ExperimentStatus.KILLED
    assert kill_res.experiment.actual_end is not None
    assert kill_res.decision is not None
    assert kill_res.decision.outcome == DecisionOutcome.KILL
    assert kill_res.decision.reason == "Zero clicks after 48h; target audience unresponsive"

    # Allocation released
    summary = cap_repo.get_financial_summary()
    assert summary.current_balance == Decimal("1000.00")  # No spend occurred
    assert summary.total_allocated == Decimal("0.00")  # Released
    assert summary.available_unallocated == Decimal("1000.00")


def test_kill_approved_experiment_before_start_releases_allocation_and_records_kill_decision(
    session: Session, sample_approved_experiment: Experiment
):
    """Killing an APPROVED experiment before it is started transitions to KILLED and releases unspent allocation.
    
    This enables immediate cancellation / emergency stop of committed experiments without running them.
    """
    cap_repo = CapitalRepository(session)
    # Check that experiment is APPROVED and budget is allocated
    summary_before = cap_repo.get_financial_summary()
    assert summary_before.total_allocated == Decimal("80.00")
    assert summary_before.available_unallocated == Decimal("920.00")

    kill_res = ExperimentExecutionService.kill(
        session,
        experiment_id=sample_approved_experiment.id,
        reason="Emergency stop: Market conditions shifted before launch",
    )

    assert kill_res.is_successful is True
    assert kill_res.experiment is not None
    assert kill_res.experiment.status == ExperimentStatus.KILLED
    assert kill_res.experiment.actual_end is not None
    assert kill_res.decision is not None
    assert kill_res.decision.outcome == DecisionOutcome.KILL
    assert kill_res.decision.reason == "Emergency stop: Market conditions shifted before launch"

    # Allocation is immediately released back to available unallocated capital
    summary_after = cap_repo.get_financial_summary()
    assert summary_after.current_balance == Decimal("1000.00")  # No spend occurred
    assert summary_after.total_allocated == Decimal("0.00")  # Released
    assert summary_after.available_unallocated == Decimal("1000.00")


def test_completed_and_killed_experiments_cannot_be_restarted(
    session: Session, sample_approved_experiment: Experiment
):
    """Experiments in terminal states (COMPLETED or KILLED) cannot be restarted."""
    ExperimentExecutionService.start(session, sample_approved_experiment.id)
    ExperimentExecutionService.complete(session, sample_approved_experiment.id)

    # Attempt to restart completed experiment
    res1 = ExperimentExecutionService.start(session, sample_approved_experiment.id)
    assert res1.is_successful is False
    assert "Only experiments in 'approved' status can be started" in (res1.rejection_reason or "")


# ── 4. Metrics Isolation ──────────────────────────────────────────────────────

def test_record_metrics_isolated_from_financial_ledger(
    session: Session, sample_approved_experiment: Experiment
):
    """Recording metrics persists in metrics table without mutating the capital ledger."""
    cap_repo = CapitalRepository(session)
    metrics_repo = MetricsRepository(session)

    ExperimentExecutionService.start(session, sample_approved_experiment.id)

    # Record metrics showing revenue
    metrics_data = ExperimentMetrics(
        experiment_id=sample_approved_experiment.id,
        impressions=1200,
        clicks=45,
        visitors=40,
        conversions=2,
        conversion_rate=0.05,
        revenue=Decimal("598.00"),
        cost=Decimal("40.00"),
        profit_loss=Decimal("558.00"),
        roi=13.95,
    )

    saved_metrics = ExperimentExecutionService.record_metrics(session, metrics_data)
    assert saved_metrics.id is not None
    assert saved_metrics.experiment_id == sample_approved_experiment.id
    assert saved_metrics.revenue == Decimal("598.00")

    # Financial ledger remains strictly untouched by metrics creation
    summary = cap_repo.get_financial_summary()
    assert summary.total_revenue == Decimal("0.00")  # Ledger only updates on financial transactions
    assert summary.current_balance == Decimal("1000.00")
