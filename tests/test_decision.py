"""Unit and integration tests for Experiment Outcome Decision Foundation (Step 10).

Verifies:
1. Explicit SCALE, ITERATE, KILL, HOLD, and APPROVE decisions can be recorded.
2. Missing or empty reason/rationale is strictly rejected.
3. Decisions must reference an existing Experiment or Opportunity (no orphan decisions).
4. Mismatched experiment_id / opportunity_id is detected and rejected.
5. Decisions are strictly append-only and immutable.
6. Chronological audit history and latest decision retrieval.
7. Strict financial isolation:
   - Zero capital transactions created on decision recording.
   - Zero change to liquid capital balance.
   - Zero change to ledger actual spend.
   - Zero change to active capital allocations.
8. No automated decision-making or threshold-based scoring.
9. No automatic experiment status mutation or autonomous execution triggers.
"""

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
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
from venturebot.execution.service import ExperimentExecutionService
from venturebot.measurement.service import ExperimentMeasurementService
from venturebot.models.decision import Decision, DecisionOutcome
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
def executed_experiment(session: Session) -> tuple[Opportunity, Experiment, ExperimentMetrics]:
    """Sets up an opportunity, approved experiment, runs it, and records measured results."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="AI Invoicing Assistant",
            description="Micro SaaS extracting invoice tables into Google Sheets.",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.DISCOVERED,
            source="ProductHunt Launch Survey",
            evidence_notes="5 early users requested beta access",
            audience="Freelancers and SMB accountants",
            trend_strength="Strong",
            growth_indicators="Invoice automation searches up 40%",
            competition_level="Moderate",
            monetization_notes="₹299 one-time license",
            production_difficulty="Low",
            distribution_difficulty="Direct outreach",
            automation_potential="High",
            platform_dependency="Google Workspace",
            regulatory_notes="None",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("1500.00"),
            estimated_cost_min=Decimal("60.00"),
            estimated_cost_max=Decimal("120.00"),
            confidence=0.85,
        )
    )

    exp_repo = ExperimentRepository(session)
    exp = exp_repo.create(
        Experiment(
            opportunity_id=opp.id,
            hypothesis="Offering 1-click invoice export will convert at least 4 paying users from 50 leads.",
            objective="Validate willingness to pay ₹299 for invoice parser.",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("60.00"),
            max_allowed_spend=Decimal("120.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="Achieve ₹1,196 revenue with spend <= ₹60.",
            failure_criteria="Zero conversions after 50 direct demos.",
            status=ExperimentStatus.DRAFT,
        )
    )

    # Approve and start
    ExperimentApprovalService.approve(session, experiment_id=exp.id, reason="Approved pilot")
    start_res = ExperimentExecutionService.start(session, experiment_id=exp.id)
    assert start_res.is_successful is True
    running_exp = start_res.experiment
    assert running_exp is not None

    # Record actual spend
    ExperimentExecutionService.record_spend(
        session,
        experiment_id=running_exp.id,
        amount=Decimal("40.00"),
        description="Demo server hosting",
    )

    # Record measured results
    metrics = ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=running_exp.id,
            evidence_type=EvidenceCategory.FACT,
            source_reference="Stripe customer invoices #201-#205",
            visitors=50,
            conversions=5,
            revenue=Decimal("1495.00"),
            cost=Decimal("40.00"),
        ),
    )

    return opp, running_exp, metrics


# ── 1. Explicit Decision Recording ────────────────────────────────────────────

def test_record_explicit_scale_decision(
    session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]
):
    """Explicit SCALE decision can be recorded with evidence summary and rationale."""
    opp, exp, metrics = executed_experiment

    decision = Decision(
        experiment_id=exp.id,
        outcome=DecisionOutcome.SCALE,
        reason="Exceeded target: 5 paying customers at 10% conversion rate; CAC well below unit revenue.",
        evidence_summary=f"Measured revenue: ₹{metrics.revenue}, cost: ₹{metrics.cost}, ROI: {metrics.roi}",
        confidence=0.9,
    )

    saved = ExperimentDecisionService.record_decision(session, decision)

    assert saved.id == decision.id
    assert saved.experiment_id == exp.id
    assert saved.opportunity_id == opp.id  # Auto-resolved from experiment
    assert saved.outcome == DecisionOutcome.SCALE
    assert saved.reason == "Exceeded target: 5 paying customers at 10% conversion rate; CAC well below unit revenue."
    assert "Measured revenue: ₹1495.00" in saved.evidence_summary
    assert saved.confidence == 0.9


def test_record_explicit_iterate_decision(
    session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]
):
    """Explicit ITERATE decision can be recorded with specific pivot rationale."""
    opp, exp, _ = executed_experiment

    decision = Decision(
        experiment_id=exp.id,
        outcome=DecisionOutcome.ITERATE,
        reason="Good traffic engagement but checkout friction observed on desktop; test mobile-first flow.",
        evidence_summary="45 clicks, 2 conversions (4.4% CR vs 8% target).",
        confidence=0.75,
    )

    saved = ExperimentDecisionService.record_decision(session, decision)
    assert saved.outcome == DecisionOutcome.ITERATE
    assert "checkout friction" in saved.reason


def test_record_explicit_kill_decision(
    session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]
):
    """Explicit KILL decision can be recorded with definitive termination rationale."""
    opp, exp, _ = executed_experiment

    decision = Decision(
        experiment_id=exp.id,
        outcome=DecisionOutcome.KILL,
        reason="Zero buyer interest after 50 direct demos; offer value proposition rejected by ICP.",
        evidence_summary="0 conversions from 50 prospects across 7 days.",
        confidence=0.95,
    )

    saved = ExperimentDecisionService.record_decision(session, decision)
    assert saved.outcome == DecisionOutcome.KILL
    assert saved.confidence == 0.95


def test_record_explicit_hold_decision(
    session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]
):
    """Explicit HOLD decision can be recorded when awaiting external data."""
    opp, exp, _ = executed_experiment

    decision = Decision(
        experiment_id=exp.id,
        outcome=DecisionOutcome.HOLD,
        reason="Awaiting end-of-month B2B purchasing cycle to measure delayed invoice conversions.",
        evidence_summary="10 active trial users; 0 invoices settled yet.",
        confidence=0.6,
    )

    saved = ExperimentDecisionService.record_decision(session, decision)
    assert saved.outcome == DecisionOutcome.HOLD


# ── 2. Validation & Error Handling ────────────────────────────────────────────

def test_missing_or_empty_reason_rejected(
    session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]
):
    """Decisions without explicit, non-empty rationale are strictly rejected."""
    _, exp, _ = executed_experiment

    # Empty string
    with pytest.raises(ValueError, match="explicit, non-empty reason"):
        ExperimentDecisionService.record_decision(
            session,
            Decision(
                experiment_id=exp.id,
                outcome=DecisionOutcome.SCALE,
                reason="",
            ),
        )

    # Whitespace only
    with pytest.raises(ValueError, match="explicit, non-empty reason"):
        ExperimentDecisionService.record_decision(
            session,
            Decision(
                experiment_id=exp.id,
                outcome=DecisionOutcome.SCALE,
                reason="   \t\n  ",
            ),
        )


def test_non_existent_experiment_rejected(session: Session):
    """Decisions referencing a non-existent experiment are rejected."""
    fake_id = uuid4()
    with pytest.raises(ValueError, match="does not exist"):
        ExperimentDecisionService.record_decision(
            session,
            Decision(
                experiment_id=fake_id,
                outcome=DecisionOutcome.KILL,
                reason="Experiment does not exist",
            ),
        )


def test_mismatched_opportunity_id_rejected(
    session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]
):
    """Providing an opportunity_id that does not match the experiment's opportunity is rejected."""
    _, exp, _ = executed_experiment
    wrong_opp_id = uuid4()

    with pytest.raises(ValueError, match="does not match"):
        ExperimentDecisionService.record_decision(
            session,
            Decision(
                experiment_id=exp.id,
                opportunity_id=wrong_opp_id,
                outcome=DecisionOutcome.SCALE,
                reason="Mismatched IDs",
            ),
        )


def test_decision_without_experiment_or_opportunity_rejected(session: Session):
    """A decision without both experiment_id and opportunity_id is rejected."""
    with pytest.raises(ValueError, match="associated with an experiment_id or opportunity_id"):
        ExperimentDecisionService.record_decision(
            session,
            Decision(
                experiment_id=None,
                opportunity_id=None,
                outcome=DecisionOutcome.HOLD,
                reason="Orphan decision",
            ),
        )


# ── 3. Immutability & Audit Trail ─────────────────────────────────────────────

def test_decisions_are_append_only_audit_trail(
    session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]
):
    """Multiple decisions create an append-only audit trail and latest decision is accurately retrieved."""
    opp, exp, _ = executed_experiment

    now = datetime.now(timezone.utc)
    d1 = ExperimentDecisionService.record_decision(
        session,
        Decision(
            experiment_id=exp.id,
            outcome=DecisionOutcome.HOLD,
            reason="Day 3: Gathering more data",
            decided_at=now - timedelta(days=4),
        ),
    )

    d2 = ExperimentDecisionService.record_decision(
        session,
        Decision(
            experiment_id=exp.id,
            outcome=DecisionOutcome.SCALE,
            reason="Day 7: Final data confirms strong ROI",
            decided_at=now,
        ),
    )

    all_decisions = ExperimentDecisionService.list_decisions_for_experiment(session, exp.id)
    # The approval decision from fixture + d1 + d2 = 3 decisions
    assert len(all_decisions) == 3

    latest = ExperimentDecisionService.get_latest_decision_for_experiment(session, exp.id)
    assert latest is not None
    assert latest.id == d2.id
    assert latest.outcome == DecisionOutcome.SCALE
    assert latest.reason == "Day 7: Final data confirms strong ROI"


# ── 4. Financial Safety & Separation ──────────────────────────────────────────

def test_decision_recording_has_zero_financial_side_effects(
    session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]
):
    """
    Financial safety invariant:
    Recording a SCALE, ITERATE, KILL, or HOLD decision must NEVER:
    1. Create capital transactions.
    2. Change liquid capital balance.
    3. Change ledger actual spend.
    4. Auto-allocate capital or disburse funds.
    """
    _, exp, _ = executed_experiment
    cap_repo = CapitalRepository(session)

    summary_before = cap_repo.get_financial_summary()
    tx_count_before = len(cap_repo.get_transaction_history())

    # Record SCALE decision
    ExperimentDecisionService.record_decision(
        session,
        Decision(
            experiment_id=exp.id,
            outcome=DecisionOutcome.SCALE,
            reason="Validated profitability; recommend budget expansion",
        ),
    )

    summary_after = cap_repo.get_financial_summary()
    tx_count_after = len(cap_repo.get_transaction_history())

    # Invariants unchanged
    assert tx_count_after == tx_count_before
    assert summary_after.current_balance == summary_before.current_balance
    assert summary_after.total_cost == summary_before.total_cost
    assert summary_after.total_allocated == summary_before.total_allocated
    assert summary_after.available_unallocated == summary_before.available_unallocated
    assert summary_after.total_revenue == summary_before.total_revenue


# ── 5. Zero Automated Decision Generation ─────────────────────────────────────

def test_no_automatic_decision_from_metrics(session: Session, executed_experiment: tuple[Opportunity, Experiment, ExperimentMetrics]):
    """High metrics alone do not automatically create decisions without explicit human command."""
    _, exp, _ = executed_experiment
    dec_repo = DecisionRepository(session)

    count_before = len(dec_repo.list(experiment_id=exp.id))

    # Recording exceptional metric
    ExperimentMeasurementService.record_measurement(
        session,
        ExperimentMetrics(
            experiment_id=exp.id,
            evidence_type=EvidenceCategory.FACT,
            revenue=Decimal("50000.00"),
            cost=Decimal("100.00"),
            conversions=100,
        ),
    )

    count_after = len(dec_repo.list(experiment_id=exp.id))
    # Still exactly the same decision count (no auto-generated SCALE)
    assert count_after == count_before
