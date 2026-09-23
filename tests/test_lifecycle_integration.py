"""End-to-End Experiment Lifecycle Integration Tests (Step 11).

Verifies the integrated flow across all VentureBot subsystems without an artificial orchestrator:
Opportunity
    ↓
Evaluation
    ↓
Proposal Generation
    ↓
Experiment Persistence (DRAFT)
    ↓
Explicit Approval & Capital Allocation
    ↓
Execution Start (RUNNING)
    ↓
Controlled Actual Spend (EXPERIMENT_SPEND)
    ↓
Result Measurement (FACT / source reference)
    ↓
Explicit Decision (SCALE / ITERATE / KILL / HOLD)
    ↓
Completion / Kill & Unspent Allocation Release

Verifies all financial and lifecycle invariants at every single stage:
- No financial transactions created during Evaluation, Proposal, Start, Measurement, or Decision.
- Capital allocation does not become a cash outflow.
- Actual spend only occurs through the controlled execution path while RUNNING.
- Measurement results are strictly observational and isolated from the ledger.
- Decisions are strictly explicit with mandatory human rationale (no automated decision triggers).
- Terminal experiments cannot restart.
"""

from collections.abc import Generator
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.approval.service import ExperimentApprovalService
from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.decision.service import ExperimentDecisionService
from venturebot.evaluation.evaluator import OpportunityEvaluator
from venturebot.evaluation.models import EvaluationStatus
from venturebot.execution.service import ExperimentExecutionService
from venturebot.measurement.service import ExperimentMeasurementService
from venturebot.models.decision import Decision, DecisionOutcome
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.experiment import ExperimentStatus
from venturebot.models.metrics import ExperimentMetrics
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus
from venturebot.proposals.generator import ProposalGenerator


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite in-memory database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


def test_complete_end_to_end_successful_lifecycle(session: Session):
    """
    Comprehensive End-to-End Test:
    Opportunity -> Evaluation -> Proposal -> Draft -> Approval -> Allocation ->
    Start -> Controlled Spend -> Measurement -> Explicit Decision -> Completion & Release.
    """
    # ── Stage 0: Initial Financial Ledger State ──────────────────────────────
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    summary_0 = cap_repo.get_financial_summary()
    assert summary_0.current_balance == Decimal("1000.00")
    assert summary_0.total_allocated == Decimal("0.00")
    assert summary_0.available_unallocated == Decimal("1000.00")
    assert summary_0.total_cost == Decimal("0.00")
    assert summary_0.total_revenue == Decimal("0.00")
    assert len(cap_repo.get_transaction_history()) == 1  # INITIAL_CAPITAL only

    # ── Stage 1: Opportunity Discovery & Persistence ─────────────────────────
    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="Resume ATS Formatter CLI",
            description="Command-line tool that cleans resumes for ATS compatibility.",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="Engineering community survey",
            evidence_notes="15 developers confirmed ATS rejection issue",
            audience="Job-seeking software engineers in India",
            trend_strength="Rising",
            growth_indicators="Resume keyword queries up 30%",
            competition_level="Moderate",
            monetization_notes="Direct license sale at ₹299 per download",
            production_difficulty="Low",
            distribution_difficulty="Direct developer community outreach via LinkedIn",
            automation_potential="High",
            platform_dependency="LinkedIn",
            regulatory_notes="None",
            estimated_revenue_min=Decimal("600.00"),
            estimated_revenue_max=Decimal("1500.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
            confidence=0.85,
        )
    )
    assert opp.id is not None
    # Invariant: No capital movements for opportunity creation
    assert len(cap_repo.get_transaction_history()) == 1

    # ── Stage 2: Opportunity Evaluation ──────────────────────────────────────
    eval_result = OpportunityEvaluator.evaluate(opp)
    assert eval_result.status == EvaluationStatus.READY_FOR_EXPERIMENT_DESIGN
    assert eval_result.has_source_evidence is True
    assert eval_result.has_economic_estimates is True
    # Invariant: No capital movements for evaluation
    assert len(cap_repo.get_transaction_history()) == 1

    # ── Stage 3: Proposal Generation ─────────────────────────────────────────
    prop_result = ProposalGenerator.generate(opp, evaluation=eval_result, timeline_days=10)
    assert prop_result.is_eligible is True
    assert prop_result.proposal is not None
    proposal = prop_result.proposal
    assert proposal.proposed_budget == Decimal("50.00")
    assert proposal.max_allowed_spend == Decimal("100.00")
    # Invariant: No capital movements for proposal generation
    assert len(cap_repo.get_transaction_history()) == 1

    # ── Stage 4: Experiment Persistence (DRAFT) ──────────────────────────────
    exp_repo = ExperimentRepository(session)
    draft_exp = proposal.to_experiment()
    persisted_exp = exp_repo.create(draft_exp)
    assert persisted_exp.status == ExperimentStatus.DRAFT
    assert persisted_exp.actual_spend == Decimal("0.00")
    # Invariant: No capital movements for experiment draft creation
    assert len(cap_repo.get_transaction_history()) == 1

    # ── Stage 5: Explicit Approval & Capital Allocation ──────────────────────
    app_result = ExperimentApprovalService.approve(
        session,
        persisted_exp.id,
        reason="Verified ATS problem demand and unit economics; approved for ₹50 pilot",
    )
    assert app_result.is_approved is True
    assert app_result.experiment is not None
    assert app_result.experiment.status == ExperimentStatus.APPROVED

    # Invariants after approval:
    # 1 allocation transaction added, liquid cash unchanged at ₹1000, unallocated reduced by ₹50
    summary_5 = cap_repo.get_financial_summary()
    assert summary_5.current_balance == Decimal("1000.00")  # Cash NOT disbursed
    assert summary_5.total_cost == Decimal("0.00")  # Spend NOT incurred
    assert summary_5.total_allocated == Decimal("50.00")  # Allocation reserved
    assert summary_5.available_unallocated == Decimal("950.00")  # ₹1000 - ₹50
    assert len(cap_repo.get_transaction_history()) == 2

    # ── Stage 6: Execution Start (RUNNING) ────────────────────────────────────
    start_result = ExperimentExecutionService.start(session, persisted_exp.id)
    assert start_result.is_successful is True
    assert start_result.experiment is not None
    assert start_result.experiment.status == ExperimentStatus.RUNNING
    assert start_result.experiment.actual_start is not None

    # Invariants after starting execution:
    # Zero capital transactions created, zero spend incurred
    summary_6 = cap_repo.get_financial_summary()
    assert summary_6.current_balance == Decimal("1000.00")
    assert summary_6.total_cost == Decimal("0.00")
    assert len(cap_repo.get_transaction_history()) == 2

    # ── Stage 7: Controlled Execution Spend ───────────────────────────────────
    spend_result = ExperimentExecutionService.record_spend(
        session,
        experiment_id=persisted_exp.id,
        amount=Decimal("35.00"),
        description="Landing page hosting & domain verification",
    )
    assert spend_result.is_successful is True
    assert spend_result.transaction is not None
    assert spend_result.experiment is not None
    assert spend_result.experiment.actual_spend == Decimal("35.00")

    # Invariants after controlled spend:
    # Cash reduced by ₹35, total cost is ₹35, remaining active allocation is ₹15 (₹50 - ₹35)
    summary_7 = cap_repo.get_financial_summary()
    assert summary_7.current_balance == Decimal("965.00")  # ₹1000 - ₹35
    assert summary_7.total_cost == Decimal("35.00")
    assert summary_7.total_allocated == Decimal("15.00")  # ₹50 budget - ₹35 spend
    assert summary_7.available_unallocated == Decimal("950.00")  # ₹965 cash - ₹15 allocated
    assert len(cap_repo.get_transaction_history()) == 3

    # ── Stage 8: Result Measurement Recording ────────────────────────────────
    metrics = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=persisted_exp.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Stripe checkout charges #101-#103",
            impressions=180,
            clicks=32,
            visitors=25,
            conversions=3,
            revenue=Decimal("897.00"),
            cost=Decimal("35.00"),
            retention_notes="3 paid downloads verified",
        ),
    )
    assert metrics.id is not None
    assert metrics.profit_loss == Decimal("862.00")  # ₹897 - ₹35
    assert metrics.roi == pytest.approx(24.63, rel=1e-2)

    # Invariants after measurement:
    # Ledger strictly unaffected by observational metrics
    summary_8 = cap_repo.get_financial_summary()
    assert summary_8.current_balance == Decimal("965.00")
    assert summary_8.total_cost == Decimal("35.00")
    assert summary_8.total_revenue == Decimal("0.00")  # Ledger only tracks financial transactions
    assert len(cap_repo.get_transaction_history()) == 3

    # ── Stage 9: Explicit Decision Recording ─────────────────────────────────
    decision = ExperimentDecisionService.record_decision(
        session,
        Decision(
            experiment_id=persisted_exp.id,
            outcome=DecisionOutcome.SCALE,
            reason="Achieved 12% conversion rate and ₹862 net profit on pilot; expand developer outreach",
            evidence_summary="3 sales from 25 visitors, ₹897 gross revenue with ₹35 cost",
            confidence=0.9,
        ),
    )
    assert decision.id is not None
    assert decision.outcome == DecisionOutcome.SCALE

    # Invariants after decision recording:
    # Zero capital transactions created, zero financial balance changes
    summary_9 = cap_repo.get_financial_summary()
    assert summary_9.current_balance == Decimal("965.00")
    assert len(cap_repo.get_transaction_history()) == 3

    # ── Stage 10: Experiment Completion & Allocation Release ─────────────────
    comp_result = ExperimentExecutionService.complete(
        session,
        experiment_id=persisted_exp.id,
        reason="Pilot period concluded with successful metrics",
        decision_outcome=DecisionOutcome.SCALE,
    )
    assert comp_result.is_successful is True
    assert comp_result.experiment is not None
    assert comp_result.experiment.status == ExperimentStatus.COMPLETED
    assert comp_result.experiment.actual_end is not None

    # Invariants after completion:
    # Remaining unspent allocation (₹15) is released back to available pool
    # Liquid cash remains ₹965 (no fake refunds), available unallocated becomes ₹965
    summary_10 = cap_repo.get_financial_summary()
    assert summary_10.current_balance == Decimal("965.00")
    assert summary_10.total_cost == Decimal("35.00")
    assert summary_10.total_allocated == Decimal("0.00")  # Released!
    assert summary_10.available_unallocated == Decimal("965.00")
    assert len(cap_repo.get_transaction_history()) == 3

    # ── Stage 11: Terminal State Protection ──────────────────────────────────
    # Terminal experiment cannot be restarted
    restart_result = ExperimentExecutionService.start(session, persisted_exp.id)
    assert restart_result.is_successful is False
    assert "Only experiments in 'approved' status can be started" in (restart_result.rejection_reason or "")


def test_complete_end_to_end_killed_experiment_lifecycle(session: Session):
    """
    End-to-End Test for a Terminated Experiment:
    Opportunity -> Evaluation -> Proposal -> Draft -> Approval -> Start -> Spend ->
    Measurement (failing) -> Explicit KILL Decision -> Kill & Allocation Release.
    """
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="Local Pet Sitter Directory",
            description="Listing site charging ₹199 for featured pet sitter profiles.",
            category=OpportunityCategory.SERVICE,
            status=OpportunityStatus.DISCOVERED,
            source="Local Facebook groups",
            evidence_notes="Verified 8 pet sitters in Bangalore area",
            audience="Independent pet sitters",
            trend_strength="Moderate",
            growth_indicators="Pet care demand steady",
            competition_level="Low",
            monetization_notes="₹199 monthly featured listing",
            production_difficulty="Low",
            distribution_difficulty="Direct WhatsApp outreach",
            automation_potential="High",
            platform_dependency="WhatsApp",
            regulatory_notes="None",
            estimated_revenue_min=Decimal("400.00"),
            estimated_revenue_max=Decimal("1000.00"),
            estimated_cost_min=Decimal("60.00"),
            estimated_cost_max=Decimal("120.00"),
            confidence=0.8,
        )
    )

    eval_result = OpportunityEvaluator.evaluate(opp)
    prop_result = ProposalGenerator.generate(opp, evaluation=eval_result)
    assert prop_result.proposal is not None
    draft_exp = prop_result.proposal.to_experiment()
    exp_repo = ExperimentRepository(session)
    persisted_exp = exp_repo.create(draft_exp)

    # Approve for ₹60 budget
    ExperimentApprovalService.approve(session, persisted_exp.id, reason="Approved test pilot")
    assert cap_repo.get_financial_summary().total_allocated == Decimal("60.00")

    # Start experiment
    ExperimentExecutionService.start(session, persisted_exp.id)

    # Spend ₹25
    ExperimentExecutionService.record_spend(
        session,
        experiment_id=persisted_exp.id,
        amount=Decimal("25.00"),
        description="Directory hosting setup",
    )
    assert cap_repo.get_financial_summary().current_balance == Decimal("975.00")

    # Record zero conversions metric
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=persisted_exp.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="WhatsApp outreach tracking sheet",
            visitors=50,
            conversions=0,
            revenue=Decimal("0.00"),
            cost=Decimal("25.00"),
            retention_notes="0 responses from 50 contacts",
        ),
    )

    # Record explicit KILL decision
    ExperimentDecisionService.record_decision(
        session,
        Decision(
            experiment_id=persisted_exp.id,
            outcome=DecisionOutcome.KILL,
            reason="Zero conversions after 50 direct contacts; market unwilling to pay for listing",
            evidence_summary="50 contacts, 0 conversions, ₹0 revenue, ₹25 spent",
        ),
    )

    # Kill experiment
    kill_result = ExperimentExecutionService.kill(
        session,
        experiment_id=persisted_exp.id,
        reason="Offer rejected by target sellers; terminating early to protect capital",
    )
    assert kill_result.is_successful is True
    assert kill_result.experiment is not None
    assert kill_result.experiment.status == ExperimentStatus.KILLED

    # Unspent allocation (₹60 - ₹25 = ₹35) is released
    summary = cap_repo.get_financial_summary()
    assert summary.current_balance == Decimal("975.00")
    assert summary.total_cost == Decimal("25.00")
    assert summary.total_allocated == Decimal("0.00")  # Fully released
    assert summary.available_unallocated == Decimal("975.00")


# ── Failure Boundary Safety Tests ─────────────────────────────────────────────

def test_failure_boundary_evaluation_blocks_insufficient_opportunity(session: Session):
    """Incomplete opportunity cannot pass evaluation and cannot generate an eligible proposal."""
    opp_repo = OpportunityRepository(session)
    incomplete_opp = opp_repo.create(
        Opportunity(
            title="Speculative Cryptocoin Analytics",
            description="Missing core evidence and economics",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="Online forum rumor",
            evidence_notes="",  # Missing evidence
            audience="Crypto traders",
            trend_strength="Unknown",
            growth_indicators="",
            competition_level="High",
            monetization_notes="",
            production_difficulty="High",
            distribution_difficulty="Unknown",
            automation_potential="Low",
            platform_dependency="Discord",
            regulatory_notes="High risk",
            estimated_revenue_min=Decimal("0.00"),
            estimated_revenue_max=Decimal("0.00"),
            estimated_cost_min=Decimal("500.00"),
            estimated_cost_max=Decimal("1500.00"),  # Exceeds starting capital
            confidence=0.1,
        )
    )

    eval_result = OpportunityEvaluator.evaluate(incomplete_opp)
    assert eval_result.status != EvaluationStatus.READY_FOR_EXPERIMENT_DESIGN

    prop_result = ProposalGenerator.generate(incomplete_opp, evaluation=eval_result)
    assert prop_result.is_eligible is False
    assert prop_result.proposal is None
    assert "not ready for proposal design" in (prop_result.rejection_reason or "")


def test_failure_boundary_approval_requires_human_reason(session: Session):
    """Approval without explicit reason is rejected and does not allocate capital."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="Valid Opportunity",
            description="Description",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="Source",
            evidence_notes="Verified demand",
            audience="Audience",
            trend_strength="High",
            growth_indicators="Growing",
            competition_level="Low",
            monetization_notes="₹200 fee",
            production_difficulty="Low",
            distribution_difficulty="Direct",
            automation_potential="High",
            platform_dependency="Web",
            regulatory_notes="None",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("1000.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
            confidence=0.8,
        )
    )
    prop = ProposalGenerator.generate(opp).proposal
    assert prop is not None
    draft_exp = ExperimentRepository(session).create(prop.to_experiment())

    # Empty reason rejected
    app_res = ExperimentApprovalService.approve(session, draft_exp.id, reason="   ")
    assert app_res.is_approved is False
    assert "Approval requires an explicit, non-empty reason" in (app_res.rejection_reason or "")

    # Zero capital allocated
    assert cap_repo.get_financial_summary().total_allocated == Decimal("0.00")


def test_failure_boundary_cannot_spend_outside_running(session: Session):
    """Cannot record spend on an APPROVED or DRAFT experiment before execution start."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="Valid Opp",
            description="Desc",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="Source",
            evidence_notes="Demand",
            audience="Audience",
            trend_strength="High",
            growth_indicators="Growth",
            competition_level="Low",
            monetization_notes="Sale",
            production_difficulty="Low",
            distribution_difficulty="Direct",
            automation_potential="High",
            platform_dependency="Web",
            regulatory_notes="None",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("1000.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
            confidence=0.8,
        )
    )
    prop_spend = ProposalGenerator.generate(opp).proposal
    assert prop_spend is not None
    draft_exp = ExperimentRepository(session).create(prop_spend.to_experiment())

    # 1. Spend on DRAFT rejected
    res_draft = ExperimentExecutionService.record_spend(session, draft_exp.id, Decimal("10.00"), "test")
    assert res_draft.is_successful is False

    # 2. Spend on APPROVED rejected
    ExperimentApprovalService.approve(session, draft_exp.id, reason="Approved")
    res_approved = ExperimentExecutionService.record_spend(session, draft_exp.id, Decimal("10.00"), "test")
    assert res_approved.is_successful is False
    assert "must be in 'running' status to record spend" in (res_approved.rejection_reason or "")


def test_failure_boundary_zero_automated_decision_from_metrics(session: Session):
    """High metrics do NOT automatically create a decision."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="Opp",
            description="Desc",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="Source",
            evidence_notes="Demand",
            audience="Audience",
            trend_strength="High",
            growth_indicators="Growth",
            competition_level="Low",
            monetization_notes="Sale",
            production_difficulty="Low",
            distribution_difficulty="Direct",
            automation_potential="High",
            platform_dependency="Web",
            regulatory_notes="None",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("1000.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
            confidence=0.8,
        )
    )
    prop_dec = ProposalGenerator.generate(opp).proposal
    assert prop_dec is not None
    draft_exp = ExperimentRepository(session).create(prop_dec.to_experiment())
    ExperimentApprovalService.approve(session, draft_exp.id, reason="Approved")
    ExperimentExecutionService.start(session, draft_exp.id)

    dec_repo = DecisionRepository(session)
    dec_count_before = len(dec_repo.list(experiment_id=draft_exp.id))

    # Record outsized revenue metric
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=draft_exp.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("10000.00"),
            cost=Decimal("20.00"),
            conversions=50,
        ),
    )

    dec_count_after = len(dec_repo.list(experiment_id=draft_exp.id))
    # Zero automatic decisions created
    assert dec_count_after == dec_count_before
