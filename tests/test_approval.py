"""Unit and integration tests for Controlled Approval and Capital Allocation Foundation (Step 7).

Verifies:
1. Draft experiment cannot be treated as approved without explicit approval.
2. Valid experiment can be explicitly approved.
3. Approval requires an appropriate reason.
4. Invalid/incomplete experiments are rejected.
5. Non-draft experiments cannot be approved.
6. Allocation cannot exceed the allowed capital ceiling.
7. Allocation cannot cause actual spend to increase.
8. Allocation does not create an EXPERIMENT_SPEND transaction.
9. Allocated capital remains distinct from actual spend and capital balance.
10. Multiple allocations cannot silently exceed available protected capital pool.
11. Decision/approval audit information is persisted correctly.
12. Rejection transitions draft experiment to KILLED with audit record.
13. Terminating an experiment releases allocated capital for future experiments.
14. Existing financial ledger balances remain correct throughout.
"""

from collections.abc import Generator
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.approval.models import ApprovalRequest
from venturebot.approval.service import ExperimentApprovalService
from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.models.capital import TransactionType
from venturebot.models.decision import Decision, DecisionOutcome
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
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
def sample_draft_experiment(session: Session, sample_opportunity: Opportunity) -> Experiment:
    """Persisted valid draft experiment."""
    repo = ExperimentRepository(session)
    exp = Experiment(
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
    return repo.create(exp)


# ── 1. Explicit Approval & Lifecycle ──────────────────────────────────────────

def test_draft_experiment_is_not_approved_by_default(sample_draft_experiment: Experiment, session: Session):
    """Draft experiments cannot be treated as approved without explicit human/service approval."""
    exp_repo = ExperimentRepository(session)
    exp = exp_repo.get(sample_draft_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.DRAFT
    assert exp.status != ExperimentStatus.APPROVED


def test_valid_experiment_explicit_approval(
    session: Session, sample_opportunity: Opportunity, sample_draft_experiment: Experiment
):
    """Valid draft experiment transitions to APPROVED, allocates capital, and creates decision audit."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()  # ₹1,000

    result = ExperimentApprovalService.approve(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="Evidence verified; unit economics are sound for pilot test.",
    )

    assert result.is_approved is True
    assert result.rejection_reason is None
    assert result.experiment is not None
    assert result.experiment.status == ExperimentStatus.APPROVED
    assert result.decision is not None
    assert result.decision.outcome == DecisionOutcome.APPROVE
    assert result.decision.reason == "Evidence verified; unit economics are sound for pilot test."
    assert result.decision.experiment_id == sample_draft_experiment.id
    assert result.decision.opportunity_id == sample_opportunity.id

    # Linked Opportunity also updated to approved
    opp_repo = OpportunityRepository(session)
    opp = opp_repo.get(sample_opportunity.id)
    assert opp is not None
    assert opp.status == OpportunityStatus.APPROVED


def test_approval_requires_meaningful_reason(session: Session, sample_draft_experiment: Experiment):
    """Attempting to approve without a valid reason is rejected."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    result = ExperimentApprovalService.approve(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="   ",  # Whitespace only
    )

    assert result.is_approved is False
    assert "Approval requires an explicit, non-empty reason" in (result.rejection_reason or "")
    
    # State remains DRAFT
    exp_repo = ExperimentRepository(session)
    exp = exp_repo.get(sample_draft_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.DRAFT


def test_cannot_approve_non_draft_experiment(
    session: Session, sample_draft_experiment: Experiment
):
    """Only experiments in DRAFT status can be approved."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    # First approval succeeds
    res1 = ExperimentApprovalService.approve(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="First approval",
    )
    assert res1.is_approved is True

    # Second approval attempt fails
    res2 = ExperimentApprovalService.approve(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="Duplicate approval attempt",
    )
    assert res2.is_approved is False
    assert "Only experiments in 'draft' status can be approved" in (res2.rejection_reason or "")


def test_cannot_approve_non_existent_experiment(session: Session):
    """Attempting to approve a non-existent experiment ID is rejected."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    non_existent_id = uuid4()
    result = ExperimentApprovalService.approve(
        session,
        experiment_id=non_existent_id,
        reason="Approval for non-existent experiment",
    )
    assert result.is_approved is False
    assert f"Experiment with ID '{non_existent_id}' does not exist" in (result.rejection_reason or "")


def test_cannot_approve_incomplete_experiment_missing_hypothesis(
    session: Session, sample_opportunity: Opportunity
):
    """Experiment missing a hypothesis is rejected during approval review."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()
    exp_repo = ExperimentRepository(session)

    # Bypass Pydantic validator by direct ORM / empty string to test approval gate robustness
    from venturebot.database.models import ExperimentORM
    incomplete_exp_orm = ExperimentORM(
        id=uuid4(),
        opportunity_id=sample_opportunity.id,
        hypothesis="",  # Empty
        objective="Valid objective",
        channel="website",
        monetization_method="direct_sale",
        allocated_budget=Decimal("50.00"),
        max_allowed_spend=Decimal("100.00"),
        actual_spend=Decimal("0.00"),
        success_criteria="Pass",
        failure_criteria="Fail",
        status="draft",
    )
    session.add(incomplete_exp_orm)
    session.commit()

    result = ExperimentApprovalService.approve(
        session,
        experiment_id=incomplete_exp_orm.id,
        reason="Approval attempt for empty hypothesis",
    )
    assert result.is_approved is False
    assert "missing a testable hypothesis" in (result.rejection_reason or "")


def test_cannot_approve_inverted_budget_spending_ceiling(
    session: Session, sample_draft_experiment: Experiment
):
    """Approval fails if requested max spend is less than requested allocated budget."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    result = ExperimentApprovalService.approve(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="Inverted budget test",
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("100.00"),  # max < allocated
    )
    assert result.is_approved is False
    assert "cannot be less than allocated_budget" in (result.rejection_reason or "")


# ── 2. Capital Protection & Allocation Separation ────────────────────────────

def test_allocation_does_not_increase_actual_spend_or_costs(
    session: Session, sample_draft_experiment: Experiment
):
    """Allocating budget does NOT increase actual spend or reduce ledger cash balance."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    initial_summary = cap_repo.get_financial_summary()
    assert initial_summary.current_balance == Decimal("1000.00")
    assert initial_summary.total_cost == Decimal("0.00")
    assert initial_summary.total_experiment_spending == Decimal("0.00")

    result = ExperimentApprovalService.approve(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="Approved with ₹80 allocation",
    )
    assert result.is_approved is True

    summary_after = cap_repo.get_financial_summary()
    # Cash balance remains ₹1,000 (money has not been disbursed)
    assert summary_after.current_balance == Decimal("1000.00")
    # Actual costs remain ₹0.00
    assert summary_after.total_cost == Decimal("0.00")
    assert summary_after.total_experiment_spending == Decimal("0.00")
    # Committed allocation is recorded
    assert summary_after.total_allocated == Decimal("80.00")
    assert summary_after.available_unallocated == Decimal("920.00")  # 1000 - 80


def test_allocation_creates_allocation_transaction_not_experiment_spend(
    session: Session, sample_draft_experiment: Experiment
):
    """Approval records EXPERIMENT_ALLOCATION in the ledger, never EXPERIMENT_SPEND."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    result = ExperimentApprovalService.approve(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="Approval budget check",
    )
    assert result.is_approved is True
    assert result.allocation_transaction is not None
    assert result.allocation_transaction.transaction_type == TransactionType.EXPERIMENT_ALLOCATION
    assert result.allocation_transaction.amount == Decimal("80.00")

    # Verify no EXPERIMENT_SPEND transactions exist
    spend_txs = cap_repo.get_transaction_history(transaction_type=TransactionType.EXPERIMENT_SPEND)
    assert len(spend_txs) == 0


def test_multiple_allocations_cannot_exceed_available_capital(
    session: Session, sample_opportunity: Opportunity
):
    """
    Concurrency/Multi-experiment safety check:
    Starting capital = ₹1,000.
    Experiment A requested allocation = ₹700 -> APPROVED.
    Experiment B requested allocation = ₹700 -> REJECTED (₹700 > ₹300 available).
    """
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()  # ₹1,000
    exp_repo = ExperimentRepository(session)

    exp_a = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Exp A",
            objective="Test A",
            channel=Channel.WEBSITE,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("700.00"),
            max_allowed_spend=Decimal("700.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Pass A",
            failure_criteria="Fail A",
            status=ExperimentStatus.DRAFT,
        )
    )

    exp_b = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Exp B",
            objective="Test B",
            channel=Channel.EMAIL,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("700.00"),
            max_allowed_spend=Decimal("700.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Pass B",
            failure_criteria="Fail B",
            status=ExperimentStatus.DRAFT,
        )
    )

    # 1. Exp A approval succeeds
    res_a = ExperimentApprovalService.approve(
        session,
        experiment_id=exp_a.id,
        reason="Approve Exp A for ₹700",
    )
    assert res_a.is_approved is True
    assert cap_repo.get_available_unallocated_capital() == Decimal("300.00")

    # 2. Exp B approval fails due to capital exhaustion
    res_b = ExperimentApprovalService.approve(
        session,
        experiment_id=exp_b.id,
        reason="Approve Exp B for ₹700",
    )
    assert res_b.is_approved is False
    assert "Insufficient available capital" in (res_b.rejection_reason or "")
    assert "available unallocated capital is ₹300.00" in (res_b.rejection_reason or "")

    # Exp B remains in DRAFT
    exp_b_fetched = exp_repo.get(exp_b.id)
    assert exp_b_fetched is not None
    assert exp_b_fetched.status == ExperimentStatus.DRAFT


def test_releasing_allocation_on_completion_restores_available_capital(
    session: Session, sample_opportunity: Opportunity
):
    """When an approved experiment is completed/killed, its unspent allocation is released."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()
    exp_repo = ExperimentRepository(session)

    exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Hypothesis",
            objective="Objective",
            channel=Channel.WEBSITE,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("400.00"),
            max_allowed_spend=Decimal("400.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Success",
            failure_criteria="Failure",
            status=ExperimentStatus.DRAFT,
        )
    )

    # Approve ₹400
    res = ExperimentApprovalService.approve(session, experiment_id=exp.id, reason="Approve ₹400")
    assert res.is_approved is True
    assert cap_repo.get_available_unallocated_capital() == Decimal("600.00")

    # Experiment completes
    exp_repo.update_status(exp.id, ExperimentStatus.COMPLETED)
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")


# ── 3. Rejection & Audit ──────────────────────────────────────────────────────

def test_explicit_rejection_transitions_to_killed_with_decision_audit(
    session: Session, sample_draft_experiment: Experiment, sample_opportunity: Opportunity
):
    """Explicitly rejecting an experiment transitions it to KILLED and persists a KILL decision."""
    result = ExperimentApprovalService.reject(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="Target CAC too high compared to estimated margins.",
    )

    assert result.is_approved is False
    assert result.experiment is not None
    assert result.experiment.status == ExperimentStatus.KILLED
    assert result.decision is not None
    assert result.decision.outcome == DecisionOutcome.KILL
    assert result.decision.reason == "Target CAC too high compared to estimated margins."
    assert result.decision.experiment_id == sample_draft_experiment.id
    assert result.decision.opportunity_id == sample_opportunity.id
    assert result.allocation_transaction is None

    # Zero ledger transactions created
    cap_repo = CapitalRepository(session)
    assert len(cap_repo.get_transaction_history(experiment_id=sample_draft_experiment.id)) == 0


# ── 4. End-to-End Accounting Integrity Lifecycle ─────────────────────────────

def test_allocation_spend_release_lifecycle_accounting_integrity(
    session: Session, sample_opportunity: Opportunity
):
    """
    Comprehensive Step 7 Accounting Integrity Audit Test:
    
    1. Initial State: Starting capital ₹1,000.
       - balance=₹1,000, allocated=₹0, unallocated=₹1,000, actual_spend=₹0, total_cost=₹0, net_profit=₹0.
    
    2. Approval & Allocation: Approve ₹300 budget.
       - EXPERIMENT_ALLOCATION tx recorded for ₹300.
       - balance=₹1,000 (cash is untouched).
       - allocated=₹300 (committed).
       - unallocated=₹700.
       - actual_spend=₹0.
       - total_cost=₹0.
    
    3. Spend Phase 1: Incur ₹100 of actual experiment spend.
       - EXPERIMENT_SPEND tx recorded for ₹100.
       - balance=₹900 (cash leaves pool).
       - remaining allocation=₹200 (₹300 - ₹100).
       - unallocated=₹700 (₹900 - ₹200).
       - actual_spend=₹100.
       - total_cost=₹100.
       - net_profit=-₹100.
    
    4. Spend Phase 2: Incur additional ₹50 of actual spend.
       - EXPERIMENT_SPEND tx recorded for ₹50.
       - balance=₹850.
       - remaining allocation=₹150 (₹300 - ₹150).
       - unallocated=₹700 (₹850 - ₹150).
       - actual_spend=₹150.
       - total_cost=₹150.
       - net_profit=-₹150.
    
    5. Termination / Completion: Experiment transitions to COMPLETED.
       - Remaining allocation (₹150) is released.
       - balance=₹850 (strictly preserved, zero cash double counting).
       - allocated=₹0.
       - unallocated=₹850 (₹850 - ₹0).
       - actual_spend=₹150 (permanently recorded).
       - total_cost=₹150.
       - net_profit=-₹150.
    """
    cap_repo = CapitalRepository(session)
    exp_repo = ExperimentRepository(session)
    dec_repo = DecisionRepository(session)

    # 1. Initial State
    cap_repo.initialize_starting_capital()
    s1 = cap_repo.get_financial_summary()
    assert s1.starting_capital == Decimal("1000.00")
    assert s1.current_balance == Decimal("1000.00")
    assert s1.total_allocated == Decimal("0.00")
    assert s1.available_unallocated == Decimal("1000.00")
    assert s1.total_cost == Decimal("0.00")
    assert s1.net_profit == Decimal("0.00")

    # Create draft experiment
    exp = exp_repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Testing resume optimization service",
            objective="Customer acquisition",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("300.00"),
            max_allowed_spend=Decimal("300.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="5 customers",
            failure_criteria="0 customers",
            status=ExperimentStatus.DRAFT,
        )
    )

    # 2. Approval & Allocation
    app_res = ExperimentApprovalService.approve(
        session,
        experiment_id=exp.id,
        reason="Approving ₹300 for LinkedIn pilot",
    )
    assert app_res.is_approved is True
    assert app_res.allocation_transaction is not None
    assert app_res.allocation_transaction.transaction_type == TransactionType.EXPERIMENT_ALLOCATION
    assert app_res.allocation_transaction.amount == Decimal("300.00")

    s2 = cap_repo.get_financial_summary()
    assert s2.current_balance == Decimal("1000.00")  # Cash pool untouched
    assert s2.total_allocated == Decimal("300.00")  # Earmarked
    assert s2.available_unallocated == Decimal("700.00")
    assert s2.total_cost == Decimal("0.00")  # No spend yet
    assert s2.net_profit == Decimal("0.00")
    assert exp_repo.get_actual_spend_from_ledger(exp.id) == Decimal("0.00")

    # 3. Spend Phase 1 (₹100)
    exp_repo.update_status(exp.id, ExperimentStatus.RUNNING)
    cap_repo.record_transaction(
        experiment_id=exp.id,
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("100.00"),
        description="First ad batch",
    )

    s3 = cap_repo.get_financial_summary()
    assert s3.current_balance == Decimal("900.00")  # ₹1,000 - ₹100
    assert s3.total_cost == Decimal("100.00")
    assert s3.total_allocated == Decimal("200.00")  # Remaining commitment (₹300 - ₹100)
    assert s3.available_unallocated == Decimal("700.00")  # ₹900 - ₹200
    assert s3.net_profit == Decimal("-100.00")
    assert exp_repo.get_actual_spend_from_ledger(exp.id) == Decimal("100.00")

    # 4. Spend Phase 2 (₹50)
    cap_repo.record_transaction(
        experiment_id=exp.id,
        transaction_type=TransactionType.EXPERIMENT_SPEND,
        amount=Decimal("50.00"),
        description="Second ad batch",
    )

    s4 = cap_repo.get_financial_summary()
    assert s4.current_balance == Decimal("850.00")  # ₹900 - ₹50
    assert s4.total_cost == Decimal("150.00")
    assert s4.total_allocated == Decimal("150.00")  # Remaining commitment (₹300 - ₹150)
    assert s4.available_unallocated == Decimal("700.00")  # ₹850 - ₹150
    assert s4.net_profit == Decimal("-150.00")
    assert exp_repo.get_actual_spend_from_ledger(exp.id) == Decimal("150.00")

    # 5. Completion / Termination (Release Remaining Allocation)
    exp_repo.update_status(exp.id, ExperimentStatus.COMPLETED)

    s5 = cap_repo.get_financial_summary()
    assert s5.current_balance == Decimal("850.00")  # Cash remains ₹850 (NO cash double-counting!)
    assert s5.total_allocated == Decimal("0.00")  # Exp 1 is no longer active; commitment released
    assert s5.available_unallocated == Decimal("850.00")  # All remaining liquid cash is available
    assert s5.total_cost == Decimal("150.00")  # Historical spend permanently preserved
    assert s5.net_profit == Decimal("-150.00")
    assert exp_repo.get_actual_spend_from_ledger(exp.id) == Decimal("150.00")


def test_decision_records_are_append_only_immutable_audit_trail(
    session: Session, sample_opportunity: Opportunity, sample_draft_experiment: Experiment
):
    """Decisions are strictly append-only audit records with no edit/delete capabilities."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()
    dec_repo = DecisionRepository(session)

    # 1. Approval Decision
    app_res = ExperimentApprovalService.approve(
        session,
        experiment_id=sample_draft_experiment.id,
        reason="Initial pilot approval",
    )
    assert app_res.is_approved is True

    # 2. Subsequent performance decision
    d2 = dec_repo.create(
        Decision(
            opportunity_id=sample_opportunity.id,
            experiment_id=sample_draft_experiment.id,
            outcome=DecisionOutcome.HOLD,
            reason="Market holiday; pause experiment for 3 days",
            evidence_summary="Clicks dropped by 80%",
        )
    )

    decisions = dec_repo.list(experiment_id=sample_draft_experiment.id)
    assert len(decisions) == 2
    # Chronological history intact
    assert any(d.outcome == DecisionOutcome.APPROVE and d.reason == "Initial pilot approval" for d in decisions)
    assert any(d.outcome == DecisionOutcome.HOLD and d.reason == "Market holiday; pause experiment for 3 days" for d in decisions)
