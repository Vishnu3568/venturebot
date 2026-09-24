"""Tests for Step 52 — Persist First Pilot As Draft Only.

Verifies:
1. Opportunity persists with canonical attributes.
2. Experiment persists linked to Opportunity in DRAFT status.
3. Proposed budget (₹200.00) is preserved without capital allocation.
4. Capital invariants: starting capital = ₹1,000.00, balance = ₹1,000.00,
   allocated capital = ₹0.00, available unallocated capital = ₹1,000.00.
5. Zero CapitalTransaction records are created by this persistence step.
6. Zero Meta write requests and zero ExternalExecution records are created.
7. Idempotent persistence prevents duplicate entity creation.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.models import DecisionORM
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.env import is_safe_mode
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus

PILOT_OPPORTUNITY_ID = UUID("63667b67-8482-519c-a498-251047e4b3ec")
PILOT_EXPERIMENT_ID = UUID("49fde874-9387-5056-934c-51a9cfca164f")
PILOT_OPPORTUNITY_TITLE = "Solopreneur Financial Workflow Guide — Problem Validation Pilot"
OLD_PILOT_DESTINATION_URL = "https://venturebot.dev/pilot/freelance-workflow"
VERIFIED_CONTROLLED_DESTINATION_URL = "https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/"
PILOT_DESTINATION_URL = VERIFIED_CONTROLLED_DESTINATION_URL


def build_candidate_opportunity(opp_id: UUID = PILOT_OPPORTUNITY_ID) -> Opportunity:
    """Build canonical reviewed Step 51.1 Opportunity model."""
    return Opportunity(
        id=opp_id,
        title=PILOT_OPPORTUNITY_TITLE,
        description=(
            "A problem-validation opportunity to test whether a targeted informational "
            "workflow guide addressing invoicing and cash-flow friction generates "
            "measurable interest among Indian independent workers."
        ),
        category=OpportunityCategory.PRODUCT,
        status=OpportunityStatus.DISCOVERED,
        source="Step 51.1 Pilot Review",
        evidence_notes=(
            "[HYPOTHESIS] Presenting a targeted informational workflow guide to Indian "
            "solopreneurs and freelancers via Meta platforms will generate link clicks to a "
            "problem-validation landing page.\n"
            "[FACT] Authoritative starting capital is ₹1,000.00; Meta ad account "
            "act_1985595022114520 is active in INR; Facebook Page 1389949167526709 access verified.\n"
            "[INFERENCE] A ₹200.00 proposed budget tests click interest without risking the "
            "₹800.00 capital reserve."
        ),
        audience="Independent freelancers, solopreneurs, and agency operators in India",
        monetization_notes=(
            "Problem validation pilot prior to monetization funnel development; "
            "revenue currently UNKNOWN/UNMEASURED."
        ),
    )


def build_candidate_experiment(
    opportunity_id: UUID,
    exp_id: UUID = PILOT_EXPERIMENT_ID,
    destination_url: str | None = PILOT_DESTINATION_URL,
) -> Experiment:
    """Build canonical reviewed Step 51.1/59 Experiment model in DRAFT status."""
    return Experiment(
        id=exp_id,
        opportunity_id=opportunity_id,
        hypothesis=(
            "Presenting a targeted informational workflow guide to Indian "
            "solopreneurs and freelancers via Meta platforms will generate "
            "link clicks to a problem-validation landing page."
        ),
        objective=(
            "Problem validation pilot measuring link click engagement for "
            "solopreneur financial workflow guide on Meta Ads."
        ),
        channel=Channel.FACEBOOK,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        destination_url=destination_url,
        allocated_budget=Decimal("200.00"),  # Proposed pilot budget ceiling
        max_allowed_spend=Decimal("200.00"),  # Hard ceiling matching proposed budget
        actual_spend=Decimal("0.00"),
        success_criteria=(
            "Observable telemetry: total spend <= ₹200.00, successful delivery and link clicks recorded. "
            "Human success threshold: NOT YET DEFINED — REQUIRES HUMAN APPROVAL."
        ),
        failure_criteria=(
            "Observable telemetry: zero delivery, policy rejection, or account billing error. "
            "Human failure threshold: NOT YET DEFINED — REQUIRES HUMAN APPROVAL."
        ),
        status=ExperimentStatus.DRAFT,
    )


def persist_draft_pilot(
    session: Session,
    opp_id: UUID = PILOT_OPPORTUNITY_ID,
    exp_id: UUID = PILOT_EXPERIMENT_ID,
    destination_url: str | None = PILOT_DESTINATION_URL,
) -> tuple[Opportunity, Experiment, bool]:
    """Idempotently persist candidate Opportunity and Experiment into the database.

    Enforces:
    - Reuses existing Opportunity if matching ID or title is found.
    - Reuses existing Experiment if matching ID or opportunity link is found.
    - Strictly persists Experiment in DRAFT status.
    - If existing Experiment destination_url differs from requested, updates it idempotently.
    - Never creates CapitalTransaction records.
    - Returns (opportunity, experiment, created_flag).
    """
    opp_repo = OpportunityRepository(session, auto_commit=True)
    exp_repo = ExperimentRepository(session, auto_commit=True)

    # 1. Check existing Opportunity
    existing_opp = opp_repo.get(opp_id)
    if existing_opp is None:
        for opp in opp_repo.list():
            if opp.title == PILOT_OPPORTUNITY_TITLE:
                existing_opp = opp
                break

    if existing_opp is not None:
        target_opp = existing_opp
        opp_created = False
    else:
        target_opp = opp_repo.create(build_candidate_opportunity(opp_id))
        opp_created = True

    # 2. Check existing Experiment
    existing_exp = exp_repo.get(exp_id, sync_spend_from_ledger=False)
    if existing_exp is None:
        for exp in exp_repo.list(opportunity_id=target_opp.id, sync_spend_from_ledger=False):
            if exp.status == ExperimentStatus.DRAFT:
                existing_exp = exp
                break

    if existing_exp is not None:
        target_exp = existing_exp
        exp_created = False
        if target_exp.destination_url != destination_url:
            updated = exp_repo.update_destination_url(target_exp.id, destination_url)
            if updated is not None:
                target_exp = updated
    else:
        target_exp = exp_repo.create(
            build_candidate_experiment(target_opp.id, exp_id, destination_url=destination_url)
        )
        exp_created = True

    return target_opp, target_exp, (opp_created or exp_created)


@pytest.fixture
def test_session() -> Session:
    """Provide isolated in-memory SQLite database session with seeded starting capital."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        cap_repo = CapitalRepository(sess)
        cap_repo.initialize_starting_capital()
        return sess


def test_step52_pilot_persists_as_draft(test_session: Session) -> None:
    """Verify candidate Opportunity and Experiment persist with exact Step 51.1 specifications."""
    opp, exp, created = persist_draft_pilot(test_session)

    assert created is True
    assert opp.id == PILOT_OPPORTUNITY_ID
    assert opp.title == PILOT_OPPORTUNITY_TITLE
    assert opp.status == OpportunityStatus.DISCOVERED
    assert opp.category == OpportunityCategory.PRODUCT
    assert "invoicing and cash-flow friction" in opp.description

    assert exp.id == PILOT_EXPERIMENT_ID
    assert exp.opportunity_id == opp.id
    assert exp.status == ExperimentStatus.DRAFT
    assert exp.channel == Channel.FACEBOOK
    assert exp.monetization_method == MonetizationMethod.DIRECT_SALE
    assert exp.destination_url == PILOT_DESTINATION_URL
    assert exp.allocated_budget == Decimal("200.00")
    assert exp.max_allowed_spend == Decimal("200.00")
    assert exp.actual_spend == Decimal("0.00")
    assert "Presenting a targeted informational workflow guide" in exp.hypothesis
    assert "NOT YET DEFINED — REQUIRES HUMAN APPROVAL" in exp.success_criteria
    assert "NOT YET DEFINED — REQUIRES HUMAN APPROVAL" in exp.failure_criteria


def test_step52_financial_invariants_preserved(test_session: Session) -> None:
    """Verify that persisting the draft pilot creates 0 transactions and allocates 0 capital."""
    cap_repo = CapitalRepository(test_session)

    # Pre-persistence check
    history_before = cap_repo.get_transaction_history()
    assert len(history_before) == 1  # Exactly the 1 seed deposit
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_total_active_allocations() == Decimal("0.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")

    # Persist draft pilot
    opp, exp, _ = persist_draft_pilot(test_session)

    # Post-persistence check
    history_after = cap_repo.get_transaction_history()
    assert len(history_after) == 1  # ZERO new capital transactions created
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_total_active_allocations() == Decimal("0.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")
    assert cap_repo.get_experiment_actual_spend(exp.id) == Decimal("0.00")


def test_step52_duplicate_idempotency_safe(test_session: Session) -> None:
    """Verify that re-invoking persistence detects existing records and avoids duplicate creation."""
    opp_repo = OpportunityRepository(test_session)
    exp_repo = ExperimentRepository(test_session)

    opp1, exp1, created1 = persist_draft_pilot(test_session)
    assert created1 is True

    opp2, exp2, created2 = persist_draft_pilot(test_session)
    assert created2 is False
    assert opp1.id == opp2.id
    assert exp1.id == exp2.id

    # Total counts in DB
    all_opps = opp_repo.list()
    assert len(all_opps) == 1

    all_exps = exp_repo.list(opportunity_id=opp1.id)
    assert len(all_exps) == 1


def test_step52_no_meta_writes_and_no_decisions_created(test_session: Session) -> None:
    """Verify that persistence step creates zero ExternalExecution, zero decisions, and leaves SAFE_MODE active."""
    opp, exp, _ = persist_draft_pilot(test_session)

    # ExternalExecution isolation
    ext_repo = ExternalExecutionRepository(test_session)
    ext_exec = ext_repo.get_by_experiment_id(exp.id)
    assert ext_exec is None

    # Decision isolation (no automatic approval decision)
    decisions = test_session.query(DecisionORM).filter_by(experiment_id=exp.id).all()
    assert len(decisions) == 0

    # SAFE_MODE remains active
    assert is_safe_mode() is True


def test_step59_destination_url_update_idempotency(test_session: Session) -> None:
    """Verify Step 59: updating experiment destination URL from old to verified controlled URL.

    Verifies:
    1. Initial persistence with old inaccessible URL.
    2. Update to verified controlled GitHub Pages URL.
    3. Experiment ID unchanged (49fde874-9387-5056-934c-51a9cfca164f).
    4. Opportunity ID unchanged (63667b67-8482-519c-a498-251047e4b3ec).
    5. Status remains strictly DRAFT.
    6. Hypothesis and budget unchanged.
    7. Zero capital transactions, zero Meta writes, zero ExternalExecution records.
    8. Re-running with same URL is fully idempotent (no unnecessary mutation).
    """
    exp_repo = ExperimentRepository(test_session)
    cap_repo = CapitalRepository(test_session)
    ext_repo = ExternalExecutionRepository(test_session)

    # 1. Seed pilot with old inaccessible URL
    opp1, exp1, created1 = persist_draft_pilot(test_session, destination_url=OLD_PILOT_DESTINATION_URL)
    assert created1 is True
    assert exp1.id == PILOT_EXPERIMENT_ID
    assert exp1.destination_url == OLD_PILOT_DESTINATION_URL
    assert exp1.status == ExperimentStatus.DRAFT

    # 2. Step 59: Update to verified controlled URL
    opp2, exp2, created2 = persist_draft_pilot(test_session, destination_url=VERIFIED_CONTROLLED_DESTINATION_URL)
    assert created2 is False  # Reused existing record, not a new experiment
    assert exp2.id == PILOT_EXPERIMENT_ID  # Experiment ID unchanged
    assert exp2.opportunity_id == PILOT_OPPORTUNITY_ID  # Opportunity ID unchanged
    assert exp2.destination_url == VERIFIED_CONTROLLED_DESTINATION_URL  # Destination updated
    assert exp2.status == ExperimentStatus.DRAFT  # Remains DRAFT
    assert exp2.hypothesis == exp1.hypothesis  # Hypothesis unchanged
    assert exp2.objective == exp1.objective  # Objective unchanged
    assert exp2.allocated_budget == Decimal("200.00")  # Budget unchanged
    assert exp2.max_allowed_spend == Decimal("200.00")  # Max spend unchanged
    assert exp2.actual_spend == Decimal("0.00")  # Actual spend remains 0

    # 3. Verify single experiment record in database (no duplicate created)
    all_exps = exp_repo.list(opportunity_id=opp2.id)
    assert len(all_exps) == 1
    assert all_exps[0].destination_url == VERIFIED_CONTROLLED_DESTINATION_URL

    # 4. Verify financial invariants untouched
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_total_active_allocations() == Decimal("0.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")
    history = cap_repo.get_transaction_history()
    assert len(history) == 1  # Only the initial seed deposit

    # 5. Verify zero Meta writes, zero external executions, SAFE_MODE active
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None
    assert is_safe_mode() is True

    # 6. Idempotency: re-running with controlled URL performs no unnecessary mutation
    opp3, exp3, created3 = persist_draft_pilot(test_session, destination_url=VERIFIED_CONTROLLED_DESTINATION_URL)
    assert created3 is False
    assert exp3.id == PILOT_EXPERIMENT_ID
    assert exp3.destination_url == VERIFIED_CONTROLLED_DESTINATION_URL
    assert len(exp_repo.list(opportunity_id=opp3.id)) == 1
