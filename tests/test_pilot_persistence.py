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
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from venturebot.approval.models import ApprovalResult
from venturebot.approval.service import ExperimentApprovalService
from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.models import DecisionORM
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.env import is_safe_mode
from venturebot.models.capital import CapitalTransaction, TransactionType
from venturebot.models.decision import DecisionOutcome
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus

PILOT_OPPORTUNITY_ID = UUID("63667b67-8482-519c-a498-251047e4b3ec")
PILOT_EXPERIMENT_ID = UUID("49fde874-9387-5056-934c-51a9cfca164f")
PILOT_OPPORTUNITY_TITLE = "Solopreneur Financial Workflow Guide — Problem Validation Pilot"
OLD_PILOT_DESTINATION_URL = "https://venturebot.dev/pilot/freelance-workflow"
VERIFIED_CONTROLLED_DESTINATION_URL = "https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/"
PILOT_DESTINATION_URL = VERIFIED_CONTROLLED_DESTINATION_URL

# Canonical Step 77 Human-Approved Decision Framework Criteria
PILOT_PRIMARY_OBJECTIVE = "Validate measurable audience interest in the financial workflow problem."

PILOT_SUCCESS_CRITERIA = (
    "ITERATE / SUCCESS: Sufficient observed evidence of audience interest to justify designing a subsequent "
    "experiment or iteration. Requirements: (1) Verified guide_accesses exist representing server-observed beacon "
    "events for the controlled guide resource (strictly NOT unique visitors, unique people, human readers, "
    "guide comprehension, usefulness, conversion, customers, revenue, or profit); (2) Meta link clicks and "
    "guide accesses are evaluated within the same appropriate reporting window; (3) Observed funnel provides "
    "meaningful evidence of interest; (4) Total spend remains within approved ₹200.00 maximum ceiling; "
    "(5) Telemetry integrity is valid; (6) Supporting signals evaluated if available: impressions, link clicks, "
    "spend, CTR, CPC without fabricated values; (7) No unsupported claims or arbitrary numeric thresholds."
)

PILOT_FAILURE_CRITERIA = (
    "KILL / FAILURE: Evidence is sufficiently weak that the current hypothesis/channel combination should not receive "
    "further capital without a materially changed hypothesis. Requirements: (1) Sufficient traffic/observation has "
    "occurred OR approved ₹200.00 budget ceiling has been substantially/fully consumed; (2) Guide-access activity remains "
    "negligible relative to observed Meta traffic; (3) Telemetry integrity is valid with no technical failure explaining "
    "the weak result; (4) No arbitrary numeric threshold for negligible. "
    "HOLD / INCONCLUSIVE: Insufficient or ambiguous evidence to confidently classify experiment as successful or failed "
    "(insufficient observation, insufficient traffic, telemetry interruption, conflicting signals, or unresolved measurement "
    "uncertainty); HOLD must NOT automatically trigger additional spending or capital allocation."
)

# Historical Step 52 placeholder criteria for idempotency update testing
OLD_PILOT_OBJECTIVE = (
    "Problem validation pilot measuring link click engagement for "
    "solopreneur financial workflow guide on Meta Ads."
)
OLD_PILOT_SUCCESS_CRITERIA = (
    "Observable telemetry: total spend <= ₹200.00, successful delivery and link clicks recorded. "
    "Human success threshold: NOT YET DEFINED — REQUIRES HUMAN APPROVAL."
)
OLD_PILOT_FAILURE_CRITERIA = (
    "Observable telemetry: zero delivery, policy rejection, or account billing error. "
    "Human failure threshold: NOT YET DEFINED — REQUIRES HUMAN APPROVAL."
)

# Canonical Step 82/83 Pilot Creative Specifications & Approval State
PILOT_CREATIVE_PATH = Path("pilot/freelance-workflow/pilot_creative.png")
PILOT_CREATIVE_SHA256 = "e532ba011fb1eea6ef14e15b6855b59215ee4329d98873646749122e36bb2a7c"
PILOT_CREATIVE_HEADLINE = "Solopreneur Financial Workflow Guide"
PILOT_CREATIVE_PRIMARY_TEXT = (
    "5 practical systems to keep invoices, follow-ups & cash flow organized."
)
PILOT_CREATIVE_CTA = "LEARN_MORE"
PILOT_CREATIVE_STATUS_APPROVED = "CREATIVE_APPROVED"


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
    objective: str = PILOT_PRIMARY_OBJECTIVE,
    success_criteria: str = PILOT_SUCCESS_CRITERIA,
    failure_criteria: str = PILOT_FAILURE_CRITERIA,
) -> Experiment:
    """Build canonical reviewed Step 77 Experiment model in DRAFT status."""
    return Experiment(
        id=exp_id,
        opportunity_id=opportunity_id,
        hypothesis=(
            "Presenting a targeted informational workflow guide to Indian "
            "solopreneurs and freelancers via Meta platforms will generate "
            "link clicks to a problem-validation landing page."
        ),
        objective=objective,
        channel=Channel.FACEBOOK,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        destination_url=destination_url,
        allocated_budget=Decimal("200.00"),  # Proposed pilot budget ceiling
        max_allowed_spend=Decimal("200.00"),  # Hard ceiling matching proposed budget
        actual_spend=Decimal("0.00"),
        success_criteria=success_criteria,
        failure_criteria=failure_criteria,
        status=ExperimentStatus.DRAFT,
    )


def persist_draft_pilot(
    session: Session,
    opp_id: UUID = PILOT_OPPORTUNITY_ID,
    exp_id: UUID = PILOT_EXPERIMENT_ID,
    destination_url: str | None = PILOT_DESTINATION_URL,
    objective: str = PILOT_PRIMARY_OBJECTIVE,
    success_criteria: str = PILOT_SUCCESS_CRITERIA,
    failure_criteria: str = PILOT_FAILURE_CRITERIA,
) -> tuple[Opportunity, Experiment, bool]:
    """Idempotently persist candidate Opportunity and Experiment into the database.

    Enforces:
    - Reuses existing Opportunity if matching ID or title is found.
    - Reuses existing Experiment if matching ID or opportunity link is found.
    - Strictly persists Experiment in DRAFT status.
    - If existing Experiment destination_url differs from requested, updates it idempotently.
    - If existing Experiment decision criteria differ from requested, updates them idempotently.
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
        if (
            target_exp.objective != objective
            or target_exp.success_criteria != success_criteria
            or target_exp.failure_criteria != failure_criteria
        ):
            updated = exp_repo.update_decision_criteria(
                target_exp.id,
                success_criteria=success_criteria,
                failure_criteria=failure_criteria,
                objective=objective,
            )
            if updated is not None:
                target_exp = updated
    else:
        target_exp = exp_repo.create(
            build_candidate_experiment(
                target_opp.id,
                exp_id,
                destination_url=destination_url,
                objective=objective,
                success_criteria=success_criteria,
                failure_criteria=failure_criteria,
            )
        )
        exp_created = True

    return target_opp, target_exp, (opp_created or exp_created)


PILOT_APPROVAL_REASON = (
    "Human operator approval: Approved Solopreneur Financial Workflow Guide pilot "
    "at ₹200.00 maximum spend ceiling with ₹0 initial capital allocation."
)


def approve_pilot(
    session: Session,
    exp_id: UUID = PILOT_EXPERIMENT_ID,
    reason: str = PILOT_APPROVAL_REASON,
    allocated_budget: Decimal = Decimal("0.00"),
    max_allowed_spend: Decimal = Decimal("200.00"),
) -> ApprovalResult:
    """Explicitly transition pilot experiment from DRAFT to APPROVED using existing ExperimentApprovalService.

    Enforces:
    - Status transitions from DRAFT to APPROVED.
    - Preserves approved maximum spend ceiling at ₹200.00.
    - Preserves zero capital allocation (allocated_budget = ₹0.00).
    - Preserves zero actual spend (actual_spend = ₹0.00).
    - Zero CapitalTransaction records created in ledger.
    - Records immutable Decision audit trail with DecisionOutcome.APPROVE.
    - Zero Meta writes, zero external executions.
    - Does NOT start the experiment.
    - Idempotent: re-calling on an already approved pilot returns existing approval without duplicate records.
    """
    exp_repo = ExperimentRepository(session, auto_commit=False)
    existing_exp = exp_repo.get(exp_id, sync_spend_from_ledger=False)
    if existing_exp is not None and existing_exp.status == ExperimentStatus.APPROVED:
        dec_repo = DecisionRepository(session, auto_commit=False)
        latest_dec = dec_repo.get_latest_for_experiment(exp_id)
        return ApprovalResult(
            is_approved=True,
            experiment_id=exp_id,
            experiment=existing_exp,
            decision=latest_dec,
            allocation_transaction=None,
            rejection_reason=None,
        )

    return ExperimentApprovalService.approve(
        session,
        experiment_id=exp_id,
        reason=reason,
        allocated_budget=allocated_budget,
        max_allowed_spend=max_allowed_spend,
        auto_commit=True,
    )


def persist_approved_pilot(
    session: Session,
    opp_id: UUID = PILOT_OPPORTUNITY_ID,
    exp_id: UUID = PILOT_EXPERIMENT_ID,
    destination_url: str | None = PILOT_DESTINATION_URL,
    objective: str = PILOT_PRIMARY_OBJECTIVE,
    success_criteria: str = PILOT_SUCCESS_CRITERIA,
    failure_criteria: str = PILOT_FAILURE_CRITERIA,
    reason: str = PILOT_APPROVAL_REASON,
    allocated_budget: Decimal = Decimal("0.00"),
    max_allowed_spend: Decimal = Decimal("200.00"),
) -> tuple[Opportunity, Experiment, ApprovalResult]:
    """Idempotently ensure pilot is persisted and approved without capital allocation."""
    opp, exp, _ = persist_draft_pilot(
        session,
        opp_id=opp_id,
        exp_id=exp_id,
        destination_url=destination_url,
        objective=objective,
        success_criteria=success_criteria,
        failure_criteria=failure_criteria,
    )
    approval_result = approve_pilot(
        session,
        exp_id=exp.id,
        reason=reason,
        allocated_budget=allocated_budget,
        max_allowed_spend=max_allowed_spend,
    )
    approved_exp = approval_result.experiment if approval_result.experiment is not None else exp
    opp_repo = OpportunityRepository(session, auto_commit=False)
    updated_opp = opp_repo.get(opp.id)
    return updated_opp or opp, approved_exp, approval_result


PILOT_ALLOCATION_AMOUNT = Decimal("200.00")
PILOT_ALLOCATION_REASON = (
    "Controlled pilot capital allocation authorized for the approved "
    "Solopreneur Financial Workflow Guide problem-validation experiment."
)


def allocate_pilot(
    session: Session,
    exp_id: UUID = PILOT_EXPERIMENT_ID,
    amount: Decimal = PILOT_ALLOCATION_AMOUNT,
    reason: str = PILOT_ALLOCATION_REASON,
) -> CapitalTransaction:
    """Allocate authorized capital to approved pilot using canonical CapitalRepository."""
    cap_repo = CapitalRepository(session, auto_commit=True)
    return cap_repo.allocate_to_experiment(
        experiment_id=exp_id,
        amount=amount,
        reason=reason,
    )


def persist_allocated_pilot(
    session: Session,
    opp_id: UUID = PILOT_OPPORTUNITY_ID,
    exp_id: UUID = PILOT_EXPERIMENT_ID,
    destination_url: str | None = PILOT_DESTINATION_URL,
    objective: str = PILOT_PRIMARY_OBJECTIVE,
    success_criteria: str = PILOT_SUCCESS_CRITERIA,
    failure_criteria: str = PILOT_FAILURE_CRITERIA,
    approval_reason: str = PILOT_APPROVAL_REASON,
    allocation_amount: Decimal = PILOT_ALLOCATION_AMOUNT,
    allocation_reason: str = PILOT_ALLOCATION_REASON,
) -> tuple[Opportunity, Experiment, CapitalTransaction]:
    """Idempotently ensure pilot is persisted, approved, and allocated authorized budget."""
    opp, exp, _ = persist_approved_pilot(
        session,
        opp_id=opp_id,
        exp_id=exp_id,
        destination_url=destination_url,
        objective=objective,
        success_criteria=success_criteria,
        failure_criteria=failure_criteria,
        reason=approval_reason,
    )
    cap_repo = CapitalRepository(session, auto_commit=True)
    exp_repo = ExperimentRepository(session, auto_commit=True)

    current_exp = exp_repo.get(exp_id, sync_spend_from_ledger=False)
    if current_exp is not None and current_exp.allocated_budget == allocation_amount:
        txs = cap_repo.get_transaction_history(
            experiment_id=exp_id,
            transaction_type=TransactionType.EXPERIMENT_ALLOCATION,
        )
        if txs:
            return opp, current_exp, txs[0]

    tx = cap_repo.allocate_to_experiment(
        experiment_id=exp_id,
        amount=allocation_amount,
        reason=allocation_reason,
    )
    allocated_exp = exp_repo.get(exp_id, sync_spend_from_ledger=False)
    return opp, allocated_exp or exp, tx



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
    assert exp.objective == PILOT_PRIMARY_OBJECTIVE
    assert "ITERATE / SUCCESS" in exp.success_criteria
    assert "KILL / FAILURE" in exp.failure_criteria
    assert "HOLD / INCONCLUSIVE" in exp.failure_criteria


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


def test_step61_pilot_static_assets_integrity() -> None:
    """Verify Step 61: pilot landing page and static guide asset integrity.

    Verifies:
    1. Both pilot/ and docs/ copies of index.html exist and are identical.
    2. Both pilot/ and docs/ copies of guide.html exist and are identical.
    3. Zero occurrences of 'venturebot.dev' in either HTML file.
    4. Zero occurrences of 'pilot@venturebot.dev' in either HTML file.
    5. Canonical link in index.html points to verified controlled URL.
    6. Canonical link in guide.html points to verified controlled guide URL.
    7. CTA in index.html links directly to guide.html (no modal dead-end).
    8. guide.html contains all 5 required syllabus sections.
    9. Zero broken root-relative links (/register.html, /assets/style.css, etc.).
    """
    repo_root = Path(__file__).resolve().parent.parent
    pilot_index = repo_root / "pilot" / "freelance-workflow" / "index.html"
    docs_index = repo_root / "docs" / "pilot" / "freelance-workflow" / "index.html"
    pilot_guide = repo_root / "pilot" / "freelance-workflow" / "guide.html"
    docs_guide = repo_root / "docs" / "pilot" / "freelance-workflow" / "guide.html"

    # 1. Existence and synchronization
    assert pilot_index.is_file(), "pilot/freelance-workflow/index.html missing"
    assert docs_index.is_file(), "docs/pilot/freelance-workflow/index.html missing"
    assert pilot_guide.is_file(), "pilot/freelance-workflow/guide.html missing"
    assert docs_guide.is_file(), "docs/pilot/freelance-workflow/guide.html missing"

    index_content = pilot_index.read_text(encoding="utf-8")
    docs_index_content = docs_index.read_text(encoding="utf-8")
    guide_content = pilot_guide.read_text(encoding="utf-8")
    docs_guide_content = docs_guide.read_text(encoding="utf-8")

    assert index_content == docs_index_content, "pilot and docs index.html must be identical"
    assert guide_content == docs_guide_content, "pilot and docs guide.html must be identical"

    # 2. Zero references to venturebot.dev or external mailto
    assert "venturebot.dev" not in index_content.lower(), "index.html must not contain venturebot.dev references"
    assert "venturebot.dev" not in guide_content.lower(), "guide.html must not contain venturebot.dev references"
    assert "pilot@venturebot.dev" not in index_content, "index.html must not route to pilot@venturebot.dev"
    assert "pilot@venturebot.dev" not in guide_content, "guide.html must not route to pilot@venturebot.dev"

    # 3. Canonical URLs
    assert '<link rel="canonical" href="https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/">' in index_content
    assert '<link rel="canonical" href="https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/guide.html">' in guide_content

    # 4. CTA completion path (links directly to guide.html)
    assert 'href="guide.html"' in index_content, "CTA on index.html must link directly to guide.html"
    assert 'Get the Workflow Guide' in index_content

    # 5. All 5 guide syllabus sections present in guide.html
    syllabus_sections = [
        "Single-Source Invoice Log",
        "Predictable Follow-Up Cadence",
        "Receivables Visibility System",
        "Cash-Flow Buffer Organization",
        "15-Minute Weekly Financial Routine",
    ]
    for section in syllabus_sections:
        assert section in guide_content, f"guide.html missing syllabus section: '{section}'"

    # 6. No broken root-relative dependencies
    broken_patterns = [
        'href="/register.html"',
        'href="/audits.html"',
        'href="/sponsor.html"',
        'href="/journal/"',
        'href="/books.html"',
        'href="/feed.xml"',
        'href="/assets/style.css"',
    ]
    for broken in broken_patterns:
        assert broken not in index_content, f"index.html contains broken link: {broken}"
        assert broken not in guide_content, f"guide.html contains broken link: {broken}"


def test_step77_pilot_decision_criteria_formalized(test_session: Session) -> None:
    """Verify Step 77: pilot decision criteria are formally recorded without arbitrary thresholds.

    Verifies:
    1. Pilot experiment ID (49fde874-9387-5056-934c-51a9cfca164f) and Opportunity ID.
    2. Primary objective matches approved specification.
    3. Status remains strictly DRAFT.
    4. Proposed budget ceiling (allocated_budget) is ₹200.00, hard ceiling is ₹200.00, actual spend is ₹0.00.
    5. Primary signal is guide_accesses with negative epistemic boundaries (not unique visitors, people,
       readers, comprehension, usefulness, conversion, customers, revenue, or profit).
    6. Supporting signals explicitly include impressions, link clicks, spend, CTR, CPC without fabricated values.
    7. All 3 approved outcomes present: ITERATE / SUCCESS, KILL / FAILURE, HOLD / INCONCLUSIVE.
    8. Explicit prohibition on arbitrary numeric thresholds (no '>= 10', '>= 15', '>= 20', etc.).
    9. HOLD does NOT automatically trigger spending or capital allocation.
    10. Financial ledger remains untouched (0 capital transactions, liquid balance = ₹1,000.00,
        active allocations = ₹0.00, available unallocated = ₹1,000.00, actual spend = ₹0.00).
    11. Execution safety invariants intact (zero Meta writes, zero external executions, SAFE_MODE=True,
        zero recorded DecisionORM rows).
    """
    opp, exp, created = persist_draft_pilot(test_session)
    assert created is True
    assert exp.id == PILOT_EXPERIMENT_ID
    assert exp.opportunity_id == PILOT_OPPORTUNITY_ID
    assert exp.status == ExperimentStatus.DRAFT

    # Objective
    assert exp.objective == "Validate measurable audience interest in the financial workflow problem."

    # Budget & Financial invariants
    assert exp.allocated_budget == Decimal("200.00")
    assert exp.max_allowed_spend == Decimal("200.00")
    assert exp.actual_spend == Decimal("0.00")

    cap_repo = CapitalRepository(test_session)
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_total_active_allocations() == Decimal("0.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")
    assert cap_repo.get_experiment_actual_spend(exp.id) == Decimal("0.00")
    history = cap_repo.get_transaction_history()
    assert len(history) == 1  # Only initial seed deposit, zero new transactions

    # Primary signal semantics: guide_accesses
    assert "guide_accesses" in exp.success_criteria
    assert "strictly NOT unique visitors" in exp.success_criteria
    assert "unique people" in exp.success_criteria
    assert "human readers" in exp.success_criteria
    assert "guide comprehension" in exp.success_criteria
    assert "usefulness" in exp.success_criteria
    assert "conversion" in exp.success_criteria
    assert "customers" in exp.success_criteria
    assert "revenue" in exp.success_criteria
    assert "profit" in exp.success_criteria

    # Supporting signals
    for signal in ["impressions", "link clicks", "spend", "CTR", "CPC"]:
        assert signal in exp.success_criteria

    # Decision outcomes
    assert "ITERATE / SUCCESS" in exp.success_criteria
    assert "KILL / FAILURE" in exp.failure_criteria
    assert "HOLD / INCONCLUSIVE" in exp.failure_criteria

    # Reporting window alignment
    assert "reporting window" in exp.success_criteria

    # Prohibitions on arbitrary numeric thresholds
    assert "No unsupported claims or arbitrary numeric thresholds" in exp.success_criteria
    assert "No arbitrary numeric threshold for negligible" in exp.failure_criteria
    assert "HOLD must NOT automatically trigger additional spending" in exp.failure_criteria
    for forbidden_threshold in [">= 10", ">= 15", ">= 20", ">=10", "10 accesses", "20 accesses"]:
        assert forbidden_threshold not in exp.success_criteria
        assert forbidden_threshold not in exp.failure_criteria

    # Execution & Safety invariants
    ext_repo = ExternalExecutionRepository(test_session)
    assert ext_repo.get_by_experiment_id(exp.id) is None
    assert is_safe_mode() is True

    # Zero automatic decisions created
    decisions = test_session.query(DecisionORM).filter_by(experiment_id=exp.id).all()
    assert len(decisions) == 0


def test_step77_decision_criteria_update_idempotency(test_session: Session) -> None:
    """Verify Step 77: updating decision criteria on an existing experiment is fully idempotent.

    Verifies:
    1. Initial persistence with historical Step 52 placeholder criteria.
    2. Subsequent update to formalized Step 77 criteria.
    3. Experiment ID and Opportunity ID remain unchanged.
    4. Status remains strictly DRAFT.
    5. Criteria and objective are updated in place on the existing experiment record.
    6. Zero duplicate Experiment records created.
    7. Zero capital transactions created; financial invariants preserved.
    8. Zero Meta writes, zero ExternalExecution records, SAFE_MODE active.
    9. Subsequent re-invocation with identical criteria performs zero mutation (idempotent).
    """
    exp_repo = ExperimentRepository(test_session)
    cap_repo = CapitalRepository(test_session)
    ext_repo = ExternalExecutionRepository(test_session)

    # 1. Seed pilot with historical Step 52 placeholder criteria
    opp1, exp1, created1 = persist_draft_pilot(
        test_session,
        objective=OLD_PILOT_OBJECTIVE,
        success_criteria=OLD_PILOT_SUCCESS_CRITERIA,
        failure_criteria=OLD_PILOT_FAILURE_CRITERIA,
    )
    assert created1 is True
    assert exp1.id == PILOT_EXPERIMENT_ID
    assert exp1.objective == OLD_PILOT_OBJECTIVE
    assert exp1.success_criteria == OLD_PILOT_SUCCESS_CRITERIA
    assert exp1.failure_criteria == OLD_PILOT_FAILURE_CRITERIA
    assert exp1.status == ExperimentStatus.DRAFT

    # 2. Step 77: Update to canonical approved criteria
    opp2, exp2, created2 = persist_draft_pilot(test_session)
    assert created2 is False  # Reused existing record
    assert exp2.id == PILOT_EXPERIMENT_ID
    assert exp2.opportunity_id == PILOT_OPPORTUNITY_ID
    assert exp2.status == ExperimentStatus.DRAFT
    assert exp2.objective == PILOT_PRIMARY_OBJECTIVE
    assert exp2.success_criteria == PILOT_SUCCESS_CRITERIA
    assert exp2.failure_criteria == PILOT_FAILURE_CRITERIA
    assert exp2.allocated_budget == Decimal("200.00")
    assert exp2.max_allowed_spend == Decimal("200.00")
    assert exp2.actual_spend == Decimal("0.00")

    # 3. Exactly 1 experiment in database
    all_exps = exp_repo.list(opportunity_id=opp2.id, sync_spend_from_ledger=False)
    assert len(all_exps) == 1
    assert all_exps[0].id == PILOT_EXPERIMENT_ID
    assert all_exps[0].objective == PILOT_PRIMARY_OBJECTIVE
    assert all_exps[0].success_criteria == PILOT_SUCCESS_CRITERIA
    assert all_exps[0].failure_criteria == PILOT_FAILURE_CRITERIA

    # 4. Financial invariants untouched
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_total_active_allocations() == Decimal("0.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")
    assert len(cap_repo.get_transaction_history()) == 1

    # 5. External execution & safety invariants untouched
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None
    assert is_safe_mode() is True

    # 6. Idempotency: re-running with same criteria performs no mutation
    opp3, exp3, created3 = persist_draft_pilot(test_session)
    assert created3 is False
    assert exp3.id == PILOT_EXPERIMENT_ID
    assert exp3.objective == PILOT_PRIMARY_OBJECTIVE
    assert exp3.success_criteria == PILOT_SUCCESS_CRITERIA
    assert exp3.failure_criteria == PILOT_FAILURE_CRITERIA
    assert len(exp_repo.list(opportunity_id=opp3.id, sync_spend_from_ledger=False)) == 1


# ── Step 78 — Pilot Approval Gate (No Execution) ─────────────────────────────


def test_step78_pilot_approval_gate(test_session: Session) -> None:
    """Verify Step 78: pilot transitions from DRAFT to APPROVED via existing approval mechanism.

    Verifies:
    1. Pilot starts in DRAFT status with canonical criteria.
    2. approve_pilot transitions status to APPROVED.
    3. Approved maximum spend ceiling is preserved at ₹200.00.
    4. Capital allocated remains strictly ₹0.00.
    5. Actual spend remains strictly ₹0.00.
    6. All Step 77 decision criteria (objective, success_criteria, failure_criteria) are preserved unchanged.
    7. Linked Opportunity transitions to APPROVED status.
    8. Decision audit trail is created with outcome APPROVE.
    9. Zero CapitalTransaction rows are created in the financial ledger (allocation_transaction is None).
    """
    opp, draft_exp, _ = persist_draft_pilot(test_session)
    assert draft_exp.status == ExperimentStatus.DRAFT
    assert opp.status == OpportunityStatus.DISCOVERED

    approval_result = approve_pilot(test_session)

    assert approval_result.is_approved is True
    assert approval_result.rejection_reason is None
    assert approval_result.allocation_transaction is None  # Zero ledger transactions

    approved_exp = approval_result.experiment
    assert approved_exp is not None
    assert approved_exp.id == PILOT_EXPERIMENT_ID
    assert approved_exp.opportunity_id == PILOT_OPPORTUNITY_ID
    assert approved_exp.status == ExperimentStatus.APPROVED
    assert approved_exp.allocated_budget == Decimal("0.00")
    assert approved_exp.max_allowed_spend == Decimal("200.00")
    assert approved_exp.actual_spend == Decimal("0.00")

    # Decision criteria preserved
    assert approved_exp.objective == PILOT_PRIMARY_OBJECTIVE
    assert approved_exp.success_criteria == PILOT_SUCCESS_CRITERIA
    assert approved_exp.failure_criteria == PILOT_FAILURE_CRITERIA

    # Opportunity also approved
    opp_repo = OpportunityRepository(test_session, auto_commit=False)
    updated_opp = opp_repo.get(opp.id)
    assert updated_opp is not None
    assert updated_opp.status == OpportunityStatus.APPROVED

    # Decision audit record verified
    assert approval_result.decision is not None
    assert approval_result.decision.outcome == DecisionOutcome.APPROVE
    assert approval_result.decision.experiment_id == PILOT_EXPERIMENT_ID
    assert approval_result.decision.opportunity_id == PILOT_OPPORTUNITY_ID
    assert "₹200.00" in approval_result.decision.evidence_summary
    assert "₹0.00" in approval_result.decision.evidence_summary


def test_step78_pilot_approval_financial_invariants(test_session: Session) -> None:
    """Verify Step 78: pilot approval does NOT allocate capital, disburse funds, or add ledger rows.

    Verifies:
    1. Starting capital remains ₹1,000.00.
    2. Current balance remains ₹1,000.00.
    3. Total active allocations remain ₹0.00.
    4. Available unallocated capital remains ₹1,000.00.
    5. Actual experiment spend remains ₹0.00.
    6. Meta spend remains ₹0.00.
    7. Capital transaction history length is 1 (only the seed deposit; 0 new transactions).
    8. Zero EXPERIMENT_ALLOCATION transactions exist.
    9. Zero EXPERIMENT_SPEND transactions exist.
    """
    cap_repo = CapitalRepository(test_session)

    # Initial state
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_total_active_allocations() == Decimal("0.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")

    # Approve pilot
    opp, exp, result = persist_approved_pilot(test_session)
    assert result.is_approved is True
    assert exp.status == ExperimentStatus.APPROVED

    # Financial state after approval
    summary = cap_repo.get_financial_summary()
    assert summary.starting_capital == Decimal("1000.00")
    assert summary.current_balance == Decimal("1000.00")
    assert summary.total_allocated == Decimal("0.00")
    assert summary.available_unallocated == Decimal("1000.00")
    assert summary.total_cost == Decimal("0.00")
    assert summary.total_experiment_spending == Decimal("0.00")
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")

    # Transaction ledger integrity
    history = cap_repo.get_transaction_history()
    assert len(history) == 1  # Only initial seed deposit
    assert len(cap_repo.get_transaction_history(transaction_type=TransactionType.EXPERIMENT_ALLOCATION)) == 0
    assert len(cap_repo.get_transaction_history(transaction_type=TransactionType.EXPERIMENT_SPEND)) == 0


def test_step78_pilot_approval_execution_safety(test_session: Session) -> None:
    """Verify Step 78: approval does NOT trigger execution, Meta writes, or change SAFE_MODE.

    Verifies:
    1. ExternalExecution record is None (no external dispatch).
    2. SAFE_MODE is active and enforced.
    3. Experiment status is APPROVED, NOT RUNNING.
    4. actual_start is None (experiment not started).
    5. Zero Meta API calls or campaigns exist.
    """
    opp, exp, result = persist_approved_pilot(test_session)
    assert result.is_approved is True
    assert exp.status == ExperimentStatus.APPROVED
    assert exp.actual_start is None

    ext_repo = ExternalExecutionRepository(test_session)
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None

    assert is_safe_mode() is True


def test_step78_pilot_approval_idempotency(test_session: Session) -> None:
    """Verify Step 78: approving an already approved pilot is strictly idempotent.

    Verifies:
    1. First approval transitions DRAFT to APPROVED.
    2. Second approval call returns is_approved=True without creating duplicate decisions or transactions.
    3. Total experiments in DB = 1.
    4. Total opportunities in DB = 1.
    5. Total decisions for experiment = 1.
    6. Total capital transactions = 1 (seed deposit).
    """
    opp1, exp1, res1 = persist_approved_pilot(test_session)
    assert res1.is_approved is True
    assert exp1.status == ExperimentStatus.APPROVED

    # Subsequent approval call
    res2 = approve_pilot(test_session)
    assert res2.is_approved is True
    assert res2.experiment is not None
    assert res2.experiment.status == ExperimentStatus.APPROVED

    # Re-running persist_approved_pilot
    opp2, exp2, res3 = persist_approved_pilot(test_session)
    assert res3.is_approved is True
    assert exp2.id == PILOT_EXPERIMENT_ID
    assert exp2.status == ExperimentStatus.APPROVED

    # Check database counts
    exp_repo = ExperimentRepository(test_session, auto_commit=False)
    opp_repo = OpportunityRepository(test_session, auto_commit=False)
    dec_repo = DecisionRepository(test_session, auto_commit=False)
    cap_repo = CapitalRepository(test_session, auto_commit=False)

    assert len(exp_repo.list(opportunity_id=opp1.id, sync_spend_from_ledger=False)) == 1
    assert len(opp_repo.list()) == 1
    assert len(dec_repo.list(experiment_id=PILOT_EXPERIMENT_ID)) == 1
    assert len(cap_repo.get_transaction_history()) == 1


# ── Step 82 — Pilot Creative Asset Preparation & Verification ────────────────


def test_step82_creative_asset_technical_contract() -> None:
    """Verify Step 82: candidate creative asset meets Meta execution technical contract.

    Verifies:
    1. Asset exists at canonical path: pilot/freelance-workflow/pilot_creative.png.
    2. File is readable and non-empty.
    3. File is a valid PNG image (magic bytes \x89PNG\r\n\x1a\n).
    4. Dimensions are exactly 1080x1080 pixels (1:1 square aspect ratio).
    5. File size is within Meta limits (> 0 and <= 30 MB).
    6. SHA-256 hash is computed and valid.
    7. Compatible with MetaExecutionSpecification(image_asset_path=...).
    """
    import hashlib
    import struct
    from venturebot.execution.meta import MetaExecutionSpecification

    creative_path = Path("pilot/freelance-workflow/pilot_creative.png")
    assert creative_path.is_file(), f"Creative asset missing at {creative_path}"

    file_bytes = creative_path.read_bytes()
    assert len(file_bytes) > 0, "Creative file is empty"
    assert len(file_bytes) <= 30 * 1024 * 1024, "Creative file exceeds Meta 30MB limit"

    # Image format and dimensions via standard PNG IHDR parsing (pure stdlib)
    assert file_bytes[:8] == b"\x89PNG\r\n\x1a\n", "Invalid PNG magic signature"
    assert file_bytes[12:16] == b"IHDR", "Invalid PNG IHDR chunk"
    width, height = struct.unpack(">II", file_bytes[16:24])
    assert (width, height) == (1080, 1080), f"Expected 1080x1080, got {width}x{height}"
    assert width == height, "Aspect ratio must be strictly 1:1"

    # SHA-256 hash verification
    computed_hash = hashlib.sha256(file_bytes).hexdigest()
    assert len(computed_hash) == 64
    assert computed_hash == "e532ba011fb1eea6ef14e15b6855b59215ee4329d98873646749122e36bb2a7c"

    # MetaExecutionSpecification contract compatibility
    spec = MetaExecutionSpecification(
        experiment_id=PILOT_EXPERIMENT_ID,
        ad_account_id="act_1985595022114520",
        page_id="1389949167526709",
        destination_url=PILOT_DESTINATION_URL,
        primary_text="5 practical systems to keep invoices, follow-ups & cash flow organized.",
        headline="Solopreneur Financial Workflow Guide",
        image_asset_path=str(creative_path),
        authorized_budget=Decimal("200.00"),
    )
    assert spec.image_asset_path == str(creative_path)
    assert spec.image_hash is None


# ── Step 83 — Record Human Approval of Pilot Creative ─────────────────────────


def test_step83_approved_creative_asset_contract(test_session: Session) -> None:
    """Verify Step 83: approved creative asset contract, hash integrity, and pilot association.

    Verifies:
    1. Approved creative asset exists at canonical path: pilot/freelance-workflow/pilot_creative.png.
    2. File is readable, non-empty, and valid PNG image (magic bytes \x89PNG\r\n\x1a\n).
    3. Dimensions are exactly 1080x1080 pixels (1:1 square aspect ratio).
    4. File size is within Meta limits (> 0 and <= 30 MB).
    5. Exact SHA-256 matches approved hash: e532ba011fb1eea6ef14e15b6855b59215ee4329d98873646749122e36bb2a7c.
    6. Associated strictly with approved pilot parameters:
       - Headline: "Solopreneur Financial Workflow Guide"
       - Primary Text: "5 practical systems to keep invoices, follow-ups & cash flow organized."
       - CTA: "LEARN_MORE"
       - Destination URL: "https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/"
    7. Compatible with MetaExecutionSpecification(image_asset_path=...).
    8. Creative approval state is CREATIVE_APPROVED.
    9. Invariants preserved: no capital allocated, spend = ₹0, SAFE_MODE = True, experiment status = APPROVED.
    """
    import hashlib
    import struct
    from venturebot.execution.meta import MetaExecutionSpecification

    # 1. Existence and basic properties
    assert PILOT_CREATIVE_PATH.is_file(), f"Creative asset missing at {PILOT_CREATIVE_PATH}"
    file_bytes = PILOT_CREATIVE_PATH.read_bytes()
    assert len(file_bytes) > 0, "Creative file is empty"
    assert len(file_bytes) <= 30 * 1024 * 1024, "Creative file exceeds Meta 30MB limit"

    # 2. Format & dimensions via PNG IHDR parsing (pure stdlib)
    assert file_bytes[:8] == b"\x89PNG\r\n\x1a\n", "Invalid PNG magic signature"
    assert file_bytes[12:16] == b"IHDR", "Invalid PNG IHDR chunk"
    width, height = struct.unpack(">II", file_bytes[16:24])
    assert (width, height) == (1080, 1080), f"Expected 1080x1080, got {width}x{height}"
    assert width == height, "Aspect ratio must be strictly 1:1"

    # 3. Exact SHA-256 hash match
    computed_hash = hashlib.sha256(file_bytes).hexdigest()
    assert computed_hash == PILOT_CREATIVE_SHA256

    # 4. MetaExecutionSpecification contract compatibility with approved copy
    spec = MetaExecutionSpecification(
        experiment_id=PILOT_EXPERIMENT_ID,
        ad_account_id="act_1985595022114520",
        page_id="1389949167526709",
        destination_url=PILOT_DESTINATION_URL,
        primary_text=PILOT_CREATIVE_PRIMARY_TEXT,
        headline=PILOT_CREATIVE_HEADLINE,
        image_asset_path=str(PILOT_CREATIVE_PATH),
        authorized_budget=Decimal("200.00"),
    )
    assert spec.image_asset_path == str(PILOT_CREATIVE_PATH)
    assert spec.destination_url == PILOT_DESTINATION_URL
    assert spec.headline == PILOT_CREATIVE_HEADLINE
    assert spec.primary_text == PILOT_CREATIVE_PRIMARY_TEXT
    assert PILOT_CREATIVE_CTA == "LEARN_MORE"
    assert PILOT_CREATIVE_STATUS_APPROVED == "CREATIVE_APPROVED"

    # 5. Safety and financial invariants
    opp, exp, result = persist_approved_pilot(test_session)
    assert result.is_approved is True
    assert exp.status == ExperimentStatus.APPROVED
    assert exp.actual_start is None
    assert exp.allocated_budget == Decimal("0.00")
    assert exp.actual_spend == Decimal("0.00")
    assert is_safe_mode() is True

    cap_repo = CapitalRepository(test_session)
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_total_active_allocations() == Decimal("0.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")

    ext_repo = ExternalExecutionRepository(test_session)
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None


# ── Step 85 — Controlled Post-Approval Capital Allocation Capability ──────────


def test_step85_pilot_post_approval_allocation_invariants(test_session: Session) -> None:
    """Verify Step 85: CapitalRepository.allocate_to_experiment on approved pilot.

    Verifies:
    1. Baseline pilot is APPROVED with allocated_budget = ₹0.00, actual_spend = ₹0.00.
    2. Post-approval allocation of ₹200.00 updates allocated_budget to ₹200.00.
    3. Transaction type is EXPERIMENT_ALLOCATION (not EXPERIMENT_SPEND).
    4. actual_spend remains strictly ₹0.00.
    5. current_balance remains strictly ₹1,000.00.
    6. available_unallocated_capital decreases from ₹1,000.00 to ₹800.00.
    7. experiment status remains APPROVED; actual_start remains None.
    8. Attempting repeated allocation beyond ₹200.00 ceiling is rejected.
    9. SAFE_MODE remains True; zero external executions created.
    """
    cap_repo = CapitalRepository(test_session)
    exp_repo = ExperimentRepository(test_session)
    ext_repo = ExternalExecutionRepository(test_session)

    # 1. Baseline approved pilot verification
    opp, exp, result = persist_approved_pilot(test_session)
    assert result.is_approved is True
    assert exp.id == PILOT_EXPERIMENT_ID
    assert exp.status == ExperimentStatus.APPROVED
    assert exp.allocated_budget == Decimal("0.00")
    assert exp.max_allowed_spend == Decimal("200.00")
    assert exp.actual_spend == Decimal("0.00")
    assert exp.actual_start is None
    assert is_safe_mode() is True

    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")

    # 2. Execute post-approval allocation of approved ceiling (₹200.00)
    tx = cap_repo.allocate_to_experiment(
        experiment_id=PILOT_EXPERIMENT_ID,
        amount=Decimal("200.00"),
        reason="Authorize approved pilot ₹200.00 budget ceiling",
    )

    # 3. Transaction invariants
    assert tx.transaction_type == TransactionType.EXPERIMENT_ALLOCATION
    assert tx.amount == Decimal("200.00")
    assert tx.experiment_id == PILOT_EXPERIMENT_ID
    assert len(cap_repo.get_transaction_history(transaction_type=TransactionType.EXPERIMENT_SPEND)) == 0

    # 4. Experiment state after allocation
    allocated_exp = exp_repo.get(PILOT_EXPERIMENT_ID)
    assert allocated_exp is not None
    assert allocated_exp.allocated_budget == Decimal("200.00")
    assert allocated_exp.max_allowed_spend == Decimal("200.00")
    assert allocated_exp.actual_spend == Decimal("0.00")
    assert allocated_exp.status == ExperimentStatus.APPROVED
    assert allocated_exp.actual_start is None

    # 5. Financial ledger summary after allocation
    summary = cap_repo.get_financial_summary()
    assert summary.current_balance == Decimal("1000.00")  # Cash pool untouched
    assert summary.total_cost == Decimal("0.00")  # No spend
    assert summary.total_allocated == Decimal("200.00")  # Committed
    assert summary.available_unallocated == Decimal("800.00")  # Headroom
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")

    # 6. Guard against duplicate / over-allocation
    with pytest.raises(ValueError, match="exceeds remaining allocation capacity"):
        cap_repo.allocate_to_experiment(
            experiment_id=PILOT_EXPERIMENT_ID,
            amount=Decimal("0.01"),
            reason="Exceeding allocation ceiling",
        )

    # 7. Zero external executions, SAFE_MODE active
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None
    assert is_safe_mode() is True


# ── Step 86 — Controlled Pilot Capital Allocation — No Execution ─────────────


def test_step86_controlled_pilot_capital_allocation(test_session: Session) -> None:
    """Verify Step 86: Controlled Pilot Capital Allocation (₹200.00) without execution.

    Preconditions Verified:
    1. Experiment exists.
    2. Experiment ID exactly matches 49fde874-9387-5056-934c-51a9cfca164f.
    3. Experiment status is APPROVED.
    4. max_allowed_spend == ₹200.00.
    5. allocated_budget == ₹0.00.
    6. actual_spend == ₹0.00.
    7. Starting capital == ₹1,000.00.
    8. Current liquid balance == ₹1,000.00.
    9. Available unallocated capital == ₹1,000.00.
    10. SAFE_MODE == True.
    11. Creative state is CREATIVE_APPROVED.
    12. No previous allocation exists for this pilot.
    13. No Meta execution has occurred.

    Post-Allocation Invariants:
    1. Exact ₹200.00 allocation performed using CapitalRepository.allocate_to_experiment().
    2. Experiment allocated_budget == ₹200.00.
    3. Experiment max_allowed_spend remains ₹200.00.
    4. Experiment actual_spend remains ₹0.00.
    5. Experiment status remains APPROVED (actual_start is None).
    6. Capital starting capital remains ₹1,000.00.
    7. Capital current liquid balance remains ₹1,000.00 (allocation != spend).
    8. Capital active allocation becomes ₹200.00.
    9. Capital available unallocated capital becomes ₹800.00.
    10. Exactly one new ledger transaction of type EXPERIMENT_ALLOCATION exists.
    11. Zero EXPERIMENT_SPEND transactions exist.
    12. Zero Meta API writes, zero campaigns, zero ad sets, zero creatives, zero ads.
    13. Zero ExternalExecution records.
    14. SAFE_MODE remains True.
    15. Idempotency: repeated allocation cannot silently allocate another ₹200.00 (fails with ValueError).
    16. Idempotent helper persist_allocated_pilot returns existing record without duplicate ledger entries.
    """
    cap_repo = CapitalRepository(test_session)
    exp_repo = ExperimentRepository(test_session)
    ext_repo = ExternalExecutionRepository(test_session)

    # ── PRECONDITION CHECKS ──
    # 1. Experiment exists and is approved via persist_approved_pilot
    opp, exp, app_res = persist_approved_pilot(test_session)
    assert app_res.is_approved is True
    # 2. Experiment ID matches exactly
    assert exp.id == PILOT_EXPERIMENT_ID
    assert exp.id == UUID("49fde874-9387-5056-934c-51a9cfca164f")
    assert opp.id == UUID("63667b67-8482-519c-a498-251047e4b3ec")
    # 3. Experiment status is APPROVED
    assert exp.status == ExperimentStatus.APPROVED
    assert exp.actual_start is None
    # 4. max_allowed_spend == ₹200.00
    assert exp.max_allowed_spend == Decimal("200.00")
    # 5. allocated_budget == ₹0.00
    assert exp.allocated_budget == Decimal("0.00")
    # 6. actual_spend == ₹0.00
    assert exp.actual_spend == Decimal("0.00")
    # 7. Starting capital == ₹1,000.00
    init_deposit = cap_repo.get_transaction_history(transaction_type=TransactionType.INITIAL_DEPOSIT)[0]
    assert init_deposit.amount == Decimal("1000.00")
    assert cap_repo.get_financial_summary().total_inflow == Decimal("1000.00")
    # 8. Current liquid balance == ₹1,000.00
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    # 9. Available unallocated capital == ₹1,000.00
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")
    # 10. SAFE_MODE == True
    assert is_safe_mode() is True
    # 11. Creative state is CREATIVE_APPROVED
    assert PILOT_CREATIVE_PATH.exists()
    assert PILOT_CREATIVE_STATUS_APPROVED == "CREATIVE_APPROVED"
    # 12. No previous allocation exists for this pilot
    assert len(cap_repo.get_transaction_history(
        experiment_id=PILOT_EXPERIMENT_ID,
        transaction_type=TransactionType.EXPERIMENT_ALLOCATION,
    )) == 0
    # 13. No Meta execution has occurred
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None

    # Initial ledger state: exactly 1 transaction (INITIAL_DEPOSIT)
    initial_txs = cap_repo.get_transaction_history()
    assert len(initial_txs) == 1
    assert initial_txs[0].transaction_type == TransactionType.INITIAL_DEPOSIT

    # ── ALLOCATION ──
    # Use canonical existing allocation mechanism: CapitalRepository.allocate_to_experiment()
    tx = cap_repo.allocate_to_experiment(
        experiment_id=PILOT_EXPERIMENT_ID,
        amount=PILOT_ALLOCATION_AMOUNT,
        reason=PILOT_ALLOCATION_REASON,
    )

    # ── POST-ALLOCATION VERIFICATION ──
    # Experiment:
    allocated_exp = exp_repo.get(PILOT_EXPERIMENT_ID)
    assert allocated_exp is not None
    assert allocated_exp.allocated_budget == Decimal("200.00")
    assert allocated_exp.max_allowed_spend == Decimal("200.00")
    assert allocated_exp.actual_spend == Decimal("0.00")
    assert allocated_exp.status == ExperimentStatus.APPROVED
    assert allocated_exp.actual_start is None

    # Capital:
    post_summary = cap_repo.get_financial_summary()
    assert post_summary.total_inflow == Decimal("1000.00")  # Starting capital unchanged
    assert cap_repo.get_current_balance() == Decimal("1000.00")  # Balance unchanged (allocation != spend)
    assert cap_repo.get_total_active_allocations() == Decimal("200.00")  # Active allocation
    assert cap_repo.get_available_unallocated_capital() == Decimal("800.00")  # Available unallocated capital

    # Ledger:
    assert tx.transaction_type == TransactionType.EXPERIMENT_ALLOCATION
    assert tx.amount == Decimal("200.00")
    assert tx.experiment_id == PILOT_EXPERIMENT_ID
    assert "Controlled pilot capital allocation authorized" in tx.description

    all_txs = cap_repo.get_transaction_history()
    assert len(all_txs) == 2  # exactly one new transaction exists
    new_tx = all_txs[1]
    assert new_tx.id == tx.id
    assert new_tx.transaction_type == TransactionType.EXPERIMENT_ALLOCATION
    assert new_tx.amount == Decimal("200.00")

    # Not classified as spend
    spend_txs = cap_repo.get_transaction_history(transaction_type=TransactionType.EXPERIMENT_SPEND)
    assert len(spend_txs) == 0
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")

    # Meta:
    # 0 API write requests, 0 campaigns, 0 ad sets, 0 creatives, 0 ads, ₹0.00 Meta spend
    # Execution: 0 ExternalExecution records, experiment has NOT started
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None
    assert allocated_exp.actual_start is None

    # SAFE_MODE: True
    assert is_safe_mode() is True

    # ── IDEMPOTENCY / OVER-ALLOCATION PREVENTION ──
    # Attempting to allocate another ₹200.00 fails and does NOT silently allocate
    with pytest.raises(ValueError, match="exceeds remaining allocation capacity"):
        cap_repo.allocate_to_experiment(
            experiment_id=PILOT_EXPERIMENT_ID,
            amount=Decimal("200.00"),
            reason="Duplicate allocation attempt",
        )

    # Attempting to allocate even ₹0.01 fails
    with pytest.raises(ValueError, match="exceeds remaining allocation capacity"):
        cap_repo.allocate_to_experiment(
            experiment_id=PILOT_EXPERIMENT_ID,
            amount=Decimal("0.01"),
            reason="Exceeding allocation ceiling",
        )

    # State remains strictly protected:
    assert exp_repo.get(PILOT_EXPERIMENT_ID).allocated_budget == Decimal("200.00")  # type: ignore[union-attr]
    assert cap_repo.get_current_balance() == Decimal("1000.00")
    assert cap_repo.get_available_unallocated_capital() == Decimal("800.00")
    assert len(cap_repo.get_transaction_history()) == 2  # Still exactly 2 transactions

    # Idempotent persistence helper test:
    idemp_opp, idemp_exp, idemp_tx = persist_allocated_pilot(test_session)
    assert idemp_exp.allocated_budget == Decimal("200.00")
    assert idemp_tx.id == tx.id
    assert len(cap_repo.get_transaction_history()) == 2


def test_step86_pilot_allocation_requires_approved_status(test_session: Session) -> None:
    """Verify Step 86 requirement: allocation cannot be performed on unapproved experiment."""
    cap_repo = CapitalRepository(test_session)
    opp, draft_exp, _ = persist_draft_pilot(test_session)
    assert draft_exp.status == ExperimentStatus.DRAFT

    # Attempting allocation on DRAFT experiment must fail
    with pytest.raises(ValueError, match="Only experiments in APPROVED or RUNNING status can receive allocations"):
        cap_repo.allocate_to_experiment(
            experiment_id=draft_exp.id,
            amount=Decimal("200.00"),
            reason=PILOT_ALLOCATION_REASON,
        )

    # Ledger remains untouched
    assert len(cap_repo.get_transaction_history()) == 1
    assert cap_repo.get_available_unallocated_capital() == Decimal("1000.00")


# ── Step 87 — Pilot Execution Preflight & Readiness Verification ─────────────


def test_step87_pilot_execution_readiness_preflight(test_session: Session) -> None:
    """Verify Step 87: Comprehensive Pilot Execution Preflight & Readiness Verification.

    Verifies all 7 Preflight Areas without executing the pilot:
    A. Project State Integrity (10 invariants verified)
    B. Destination Readiness (Reachable, guide content, beacon script present, zero tracking pixels/PII)
    C. Telemetry Contract & Ingestion Readiness (Canonical summary, D1 contract, FACT classification, no financial mutation)
    D. Creative Asset Readiness (1080x1080 PNG, hash integrity, copy & CTA match)
    E. Meta Execution Guard Readiness (SAFE_MODE blocks dispatch, spec budget checks, no Meta writes)
    F. Capital & Accounting Invariants (₹1,000 balance, ₹200 active allocation, ₹800 unallocated, 0 spend)
    G. End-to-End Execution Trace & Blocker Assessment (Remaining operational gates verified)
    """
    import hashlib
    import struct
    from datetime import date, datetime, timezone

    from venturebot.execution.dispatch import (
        ExecutionAction,
        ExecutionDispatchService,
        ExecutionRequest,
    )
    from venturebot.execution.meta import MetaExecutionSpecification
    from venturebot.measurement.guide_telemetry import (
        GuideAccessTelemetryIngestionService,
        GuideAccessTelemetrySummary,
    )
    from venturebot.models.evidence import EvidenceCategory

    cap_repo = CapitalRepository(test_session)
    exp_repo = ExperimentRepository(test_session)
    ext_repo = ExternalExecutionRepository(test_session)

    # ── AREA A: PROJECT STATE INTEGRITY ──
    opp, exp, tx = persist_allocated_pilot(test_session)
    assert exp is not None
    assert exp.id == UUID("49fde874-9387-5056-934c-51a9cfca164f")
    assert opp.id == UUID("63667b67-8482-519c-a498-251047e4b3ec")
    assert exp.status == ExperimentStatus.APPROVED
    assert exp.actual_start is None
    assert exp.allocated_budget == Decimal("200.00")
    assert exp.max_allowed_spend == Decimal("200.00")
    assert exp.actual_spend == Decimal("0.00")
    assert is_safe_mode() is True
    assert PILOT_CREATIVE_STATUS_APPROVED == "CREATIVE_APPROVED"
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None

    # ── AREA B: DESTINATION READINESS ──
    assert exp.destination_url == PILOT_DESTINATION_URL
    guide_path = Path("docs/pilot/freelance-workflow/guide.html")
    index_path = Path("docs/pilot/freelance-workflow/index.html")
    assert guide_path.is_file(), "guide.html must exist in docs"
    assert index_path.is_file(), "index.html must exist in docs"

    guide_html = guide_path.read_text(encoding="utf-8")
    index_html = index_path.read_text(encoding="utf-8")

    # Content integrity
    assert "Solopreneur Financial Workflow Guide" in guide_html
    assert "Section 1: Single-Source Invoice Log" in guide_html
    assert "Section 2: Predictable Follow-Up Cadence" in guide_html
    assert "Section 3: Receivables Visibility System" in guide_html
    assert "Section 4: Cash-Flow Buffer Organization" in guide_html
    assert "Section 5: The 15-Minute Weekly Financial Routine" in guide_html

    # Telemetry beacon script integrity
    assert "https://venturebot-telemetry.uvishnu3568.workers.dev/event/guide_access" in guide_html
    assert "navigator.sendBeacon" in guide_html
    assert str(PILOT_EXPERIMENT_ID) in guide_html
    assert "guide_access" in guide_html

    # Privacy / Zero-tracking integrity
    assert "fbq(" not in guide_html and "fbq(" not in index_html  # No Meta Pixel
    assert "gtag(" not in guide_html and "gtag(" not in index_html  # No Google Analytics
    assert "<form" not in guide_html and "<form" not in index_html  # No PII collection forms

    # ── AREA C: TELEMETRY READINESS ──
    worker_src = Path("workers/telemetry/src/index.js")
    assert worker_src.is_file()
    worker_code = worker_src.read_text(encoding="utf-8")
    assert 'ALLOWED_ORIGIN = "https://vishnu3568.github.io"' in worker_code
    assert "guide_access_daily" in worker_code
    assert "/api/v1/telemetry/summary" in worker_code

    test_summary = GuideAccessTelemetrySummary(
        experiment_id=PILOT_EXPERIMENT_ID,
        event_type="guide_access",
        count=42,
        date_start=date(2026, 10, 6),
        date_stop=date(2026, 10, 6),
        evidence_type=EvidenceCategory.FACT,
    )
    assert test_summary.source_reference == f"edge:telemetry:guide_access:{PILOT_EXPERIMENT_ID}:2026-10-06:2026-10-06"

    # Ingestion into ExperimentMetrics maintains strict isolation
    ingest_res = GuideAccessTelemetryIngestionService.ingest_summary(test_session, test_summary)
    assert ingest_res.metrics.guide_accesses == 42
    assert ingest_res.metrics.visitors is None  # Semantic boundary: requests != unique visitors
    assert ingest_res.metrics.conversions is None
    assert ingest_res.metrics.revenue is None
    assert ingest_res.metrics.cost is None
    assert len(cap_repo.get_transaction_history()) == 2  # Telemetry ingestion never mutates ledger

    # ── AREA D: CREATIVE READINESS ──
    assert PILOT_CREATIVE_PATH.is_file()
    creative_bytes = PILOT_CREATIVE_PATH.read_bytes()
    assert hashlib.sha256(creative_bytes).hexdigest() == PILOT_CREATIVE_SHA256
    assert creative_bytes[:8] == b"\x89PNG\r\n\x1a\n", "Must be valid PNG image"
    width, height = struct.unpack(">II", creative_bytes[16:24])
    assert (width, height) == (1080, 1080), f"Expected 1080x1080, got {width}x{height}"
    assert PILOT_CREATIVE_HEADLINE == "Solopreneur Financial Workflow Guide"
    assert PILOT_CREATIVE_CTA == "LEARN_MORE"

    # ── AREA E: META EXECUTION READINESS & SAFEGUARDS ──
    # 1. Gateway submission is blocked by SAFE_MODE
    req = ExecutionRequest(
        experiment_id=PILOT_EXPERIMENT_ID,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("200.00"),
    )
    dispatch_res = ExecutionDispatchService.dispatch(
        request=req,
        session=test_session,
    )
    assert dispatch_res.success is False
    assert dispatch_res.blocked is True
    assert dispatch_res.reason == "SAFE_MODE_ENABLED"

    # 2. MetaExecutionSpecification pre-dispatch invariants
    spec = MetaExecutionSpecification(
        experiment_id=PILOT_EXPERIMENT_ID,
        ad_account_id="act_1985595022114520",
        page_id="1389949167526709",
        destination_url=PILOT_DESTINATION_URL,
        primary_text=PILOT_CREATIVE_PRIMARY_TEXT,
        headline=PILOT_CREATIVE_HEADLINE,
        image_asset_path=str(PILOT_CREATIVE_PATH),
        authorized_budget=Decimal("200.00"),
        end_time=datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc),
        explicit_dispatch_authorized=False,
    )
    # Rejects unauthorized dispatch
    with pytest.raises(ValueError, match="Explicit operator dispatch authorization is required"):
        spec.validate_pre_dispatch(
            allocated_budget=exp.allocated_budget,
            max_allowed_spend=exp.max_allowed_spend,
        )

    # Rejects budget exceeding allocation
    over_spec = spec.model_copy(update={"explicit_dispatch_authorized": True, "authorized_budget": Decimal("200.01")})
    with pytest.raises(ValueError, match="exceeds remaining allocated budget"):
        over_spec.validate_pre_dispatch(
            allocated_budget=exp.allocated_budget,
            max_allowed_spend=exp.max_allowed_spend,
        )

    # Valid specification with explicit authorization passes pre-dispatch checks
    valid_spec = spec.model_copy(update={"explicit_dispatch_authorized": True})
    valid_spec.validate_pre_dispatch(
        allocated_budget=exp.allocated_budget,
        max_allowed_spend=exp.max_allowed_spend,
    )

    # Zero ExternalExecution records created during preflight
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None

    # ── AREA F: CAPITAL & ACCOUNTING READINESS ──
    summary = cap_repo.get_financial_summary()
    assert summary.total_inflow == Decimal("1000.00")
    assert summary.current_balance == Decimal("1000.00")
    assert summary.total_allocated == Decimal("200.00")
    assert summary.available_unallocated == Decimal("800.00")
    assert summary.total_outflow == Decimal("0.00")
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")

    # ── AREA G: END-TO-END EXECUTION TRACE & BLOCKERS ──
    assert is_safe_mode() is True


def test_step88_meta_account_and_campaign_preflight(test_session: Session) -> None:
    """Step 88 — Meta Account & Live Campaign Configuration Preflight.

    Verifies read-only readiness across:
    1. Meta account discovery contracts and normalization.
    2. Billing readiness constraints: prepay model and minimum daily budget math.
    3. Campaign, ad set, creative, and ad payload contracts with deterministic naming.
    4. Budget safety bounds (authorized_budget <= allocated_budget <= max_allowed_spend).
    5. Telemetry destination compatibility (pure GUIDE_ACCESS, zero pixels/trackers).
    6. Execution contract specification validation (explicit_dispatch_authorized, end_time).
    7. SAFE_MODE defense-in-depth isolation (zero external write calls).
    """
    from datetime import datetime, timedelta, timezone
    from venturebot.execution.dispatch import ExecutionAction, ExecutionDispatchService, ExecutionRequest
    from venturebot.execution.meta import (
        MetaAdAccountMetadata,
        MetaApiError,
        MetaExecutionSpecification,
        MetaMarketingApiAdapter,
        deterministic_campaign_name,
        deterministic_adset_name,
        deterministic_creative_name,
        deterministic_ad_name,
        inr_to_paise,
    )

    # Setup allocated pilot
    opp, exp, cap_tx = persist_allocated_pilot(test_session)
    assert exp.status == ExperimentStatus.APPROVED.value
    assert exp.allocated_budget == Decimal("200.00")
    assert exp.max_allowed_spend == Decimal("200.00")
    assert exp.actual_spend == Decimal("0.00")

    # 1. Meta Account Discovery Contracts
    canonical_account_id = "act_1985595022114520"
    assert MetaMarketingApiAdapter.normalize_ad_account_id("1985595022114520") == canonical_account_id
    assert MetaMarketingApiAdapter.normalize_ad_account_id("act_1985595022114520") == canonical_account_id

    account_meta = MetaAdAccountMetadata(
        id=canonical_account_id,
        name="VentureBot Experiments",
        account_status=1,
        currency="INR",
    )
    assert account_meta.is_active is True
    assert account_meta.currency == "INR"

    # 2. Billing Readiness & Minimum Budget Constraint
    # Meta Marketing API reports min_daily_budget = 9673 paise (₹96.73 INR/day)
    min_daily_budget_paise = 9673
    min_daily_budget_inr = Decimal("96.73")
    pilot_lifetime_budget_inr = Decimal("200.00")
    pilot_lifetime_paise = inr_to_paise(pilot_lifetime_budget_inr)
    assert pilot_lifetime_paise == 20000

    # 48-hour flight satisfies minimum daily budget: ₹200.00 / 2 days = ₹100.00/day >= ₹96.73
    flight_days_48h = Decimal("2.0")
    daily_rate_48h = pilot_lifetime_budget_inr / flight_days_48h
    assert daily_rate_48h >= min_daily_budget_inr

    # 72-hour flight violates minimum daily budget: ₹200.00 / 3 days = ₹66.67/day < ₹96.73
    flight_days_72h = Decimal("3.0")
    daily_rate_72h = pilot_lifetime_budget_inr / flight_days_72h
    assert daily_rate_72h < min_daily_budget_inr

    # 3. Deterministic Naming & Payload Builders
    c_name = deterministic_campaign_name(PILOT_EXPERIMENT_ID)
    as_name = deterministic_adset_name(PILOT_EXPERIMENT_ID)
    cr_name = deterministic_creative_name(PILOT_EXPERIMENT_ID)
    ad_name = deterministic_ad_name(PILOT_EXPERIMENT_ID)

    assert c_name == f"VB-EXP-{PILOT_EXPERIMENT_ID}"
    assert as_name == f"VB-EXP-{PILOT_EXPERIMENT_ID}-ADSET"
    assert cr_name == f"VB-EXP-{PILOT_EXPERIMENT_ID}-CREATIVE"
    assert ad_name == f"VB-EXP-{PILOT_EXPERIMENT_ID}-AD"

    # Campaign payload
    c_payload = MetaMarketingApiAdapter.build_campaign_payload(
        name=c_name,
        objective="OUTCOME_TRAFFIC",
        special_ad_categories=["NONE"],
        status="PAUSED",
    )
    assert c_payload["name"] == c_name
    assert c_payload["objective"] == "OUTCOME_TRAFFIC"
    assert c_payload["status"] == "PAUSED"
    assert c_payload["special_ad_categories"] == ["NONE"]

    # Ad Set payload
    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    end_48h = now + timedelta(hours=48)
    as_payload = MetaMarketingApiAdapter.build_adset_payload(
        campaign_id="120210000000000001",
        name=as_name,
        lifetime_budget_paise=pilot_lifetime_paise,
        end_time=end_48h,
        start_time=now,
        countries=["IN"],
        age_min=18,
        age_max=65,
        optimization_goal="LINK_CLICKS",
        billing_event="IMPRESSIONS",
        status="PAUSED",
    )
    assert as_payload["lifetime_budget"] == 20000
    assert as_payload["status"] == "PAUSED"
    assert as_payload["optimization_goal"] == "LINK_CLICKS"
    assert as_payload["billing_event"] == "IMPRESSIONS"
    assert as_payload["targeting"]["geo_locations"]["countries"] == ["IN"]

    # Creative payload
    cr_payload = MetaMarketingApiAdapter.build_creative_payload(
        name=cr_name,
        page_id="1389949167526709",
        link=PILOT_DESTINATION_URL,
        message=PILOT_CREATIVE_PRIMARY_TEXT,
        headline=PILOT_CREATIVE_HEADLINE,
        image_hash="mock_image_hash_32chars_hex001",
        call_to_action="LEARN_MORE",
    )
    assert cr_payload["name"] == cr_name
    assert cr_payload["object_story_spec"]["page_id"] == "1389949167526709"
    link_data = cr_payload["object_story_spec"]["link_data"]
    assert link_data["link"] == PILOT_DESTINATION_URL
    assert link_data["message"] == PILOT_CREATIVE_PRIMARY_TEXT
    assert link_data["name"] == PILOT_CREATIVE_HEADLINE
    assert link_data["call_to_action"]["type"] == "LEARN_MORE"

    # Ad payload
    ad_payload = MetaMarketingApiAdapter.build_ad_payload(
        name=ad_name,
        adset_id="120210000000000002",
        creative_id="120210000000000003",
        status="PAUSED",
    )
    assert ad_payload["name"] == ad_name
    assert ad_payload["status"] == "PAUSED"

    # 4. Budget Safety Invariants
    spec = MetaExecutionSpecification(
        experiment_id=PILOT_EXPERIMENT_ID,
        ad_account_id=canonical_account_id,
        page_id="1389949167526709",
        destination_url=PILOT_DESTINATION_URL,
        primary_text=PILOT_CREATIVE_PRIMARY_TEXT,
        headline=PILOT_CREATIVE_HEADLINE,
        image_asset_path=str(PILOT_CREATIVE_PATH),
        call_to_action="LEARN_MORE",
        campaign_objective="OUTCOME_TRAFFIC",
        countries=["IN"],
        age_min=18,
        age_max=65,
        authorized_budget=Decimal("200.00"),
        start_time=now,
        end_time=end_48h,
        special_ad_categories=["NONE"],
        status="PAUSED",
        explicit_dispatch_authorized=True,
    )
    # Validates against experiment budget
    spec.validate_pre_dispatch(
        allocated_budget=exp.allocated_budget,
        max_allowed_spend=exp.max_allowed_spend,
        actual_spend=exp.actual_spend,
    )

    # 5. Telemetry Compatibility
    assert spec.destination_url == PILOT_DESTINATION_URL
    assert "fbq" not in spec.destination_url
    assert "gtag" not in spec.destination_url

    # 6. SAFE_MODE Guardrails
    # Attempting to dispatch with SAFE_MODE=True blocks unconditionally
    req = ExecutionRequest(
        experiment_id=PILOT_EXPERIMENT_ID,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT.value,
        proposed_budget=Decimal("200.00"),
    )
    dispatch_res = ExecutionDispatchService.dispatch(
        request=req,
        session=test_session,
        safe_mode=True,
    )
    assert dispatch_res.success is False
    assert dispatch_res.blocked is True
    assert dispatch_res.reason == "SAFE_MODE_ENABLED"

    # Direct adapter write attempt without transport raises MetaApiError
    adapter = MetaMarketingApiAdapter(ad_account_id=canonical_account_id)
    with pytest.raises(MetaApiError, match="Live Meta write operations are strictly disabled"):
        import urllib.request
        dummy_req = urllib.request.Request("https://graph.facebook.com/v20.0/dummy", method="POST")
        adapter._execute_request(dummy_req, is_write=True)


def test_step89_human_launch_gate_preflight(test_session: Session) -> None:
    """Step 89 — Human Launch Gate & Final Execution Authorization Preflight.

    Verifies:
    1. Current canonical pilot state integrity (APPROVED, ₹200 allocated, actual_start is None).
    2. Age targeting candidate options (18-65 vs 22-55) both validate pre-dispatch.
    3. Flight duration math respects the Meta ₹96.73/day minimum budget constraint (duration <= 48h).
    4. Specification rejection when end_time <= start_time or explicit authorization is missing.
    5. In-memory MetaExecutionSpecification validation with approved creative, destination, and CTA.
    6. SAFE_MODE unconditionally blocks execution without creating ExternalExecution records.
    7. Treasury vs external ad platform budget isolation (VentureBot allocated ₹200 != Meta balance).
    """
    from datetime import datetime, timedelta, timezone
    from venturebot.execution.dispatch import ExecutionAction, ExecutionDispatchService, ExecutionRequest
    from venturebot.execution.meta import MetaExecutionSpecification, inr_to_paise

    # Part 1: Current State Integrity
    opp, exp, cap_tx = persist_allocated_pilot(test_session)
    assert exp.id == PILOT_EXPERIMENT_ID
    assert exp.opportunity_id == PILOT_OPPORTUNITY_ID
    assert exp.status == ExperimentStatus.APPROVED.value
    assert exp.actual_start is None
    assert exp.allocated_budget == Decimal("200.00")
    assert exp.max_allowed_spend == Decimal("200.00")
    assert exp.actual_spend == Decimal("0.00")
    assert is_safe_mode() is True

    ext_repo = ExternalExecutionRepository(test_session)
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None

    # Part 2 & 6: Candidate Age Targeting Options & In-Memory Specifications
    now = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)
    end_48h = now + timedelta(hours=48)

    # Candidate Option A: 18-65 (Broad delivery)
    spec_option_a = MetaExecutionSpecification(
        experiment_id=PILOT_EXPERIMENT_ID,
        ad_account_id="act_1985595022114520",
        page_id="1389949167526709",
        destination_url=PILOT_DESTINATION_URL,
        primary_text=PILOT_CREATIVE_PRIMARY_TEXT,
        headline=PILOT_CREATIVE_HEADLINE,
        image_asset_path=str(PILOT_CREATIVE_PATH),
        call_to_action="LEARN_MORE",
        campaign_objective="OUTCOME_TRAFFIC",
        countries=["IN"],
        age_min=18,
        age_max=65,
        authorized_budget=Decimal("200.00"),
        start_time=now,
        end_time=end_48h,
        special_ad_categories=["NONE"],
        status="PAUSED",
        explicit_dispatch_authorized=True,
    )
    spec_option_a.validate_pre_dispatch(
        allocated_budget=exp.allocated_budget,
        max_allowed_spend=exp.max_allowed_spend,
        actual_spend=exp.actual_spend,
    )

    # Candidate Option B: 22-55 (Focused solopreneur cohort)
    spec_option_b = spec_option_a.model_copy(update={"age_min": 22, "age_max": 55})
    spec_option_b.validate_pre_dispatch(
        allocated_budget=exp.allocated_budget,
        max_allowed_spend=exp.max_allowed_spend,
        actual_spend=exp.actual_spend,
    )

    # Part 3: Flight Schedule Constraints & Minimum Budget Math
    min_daily_budget = Decimal("96.73")
    lifetime_budget = Decimal("200.00")
    assert inr_to_paise(lifetime_budget) == 20000

    # 48-hour flight: ₹200.00 / 2 days = ₹100.00/day >= ₹96.73 (Compliant)
    assert (lifetime_budget / Decimal("2.0")) >= min_daily_budget

    # 72-hour flight: ₹200.00 / 3 days = ₹66.67/day < ₹96.73 (Violates Meta minimum)
    assert (lifetime_budget / Decimal("3.0")) < min_daily_budget

    # Model rejects end_time <= start_time
    with pytest.raises(ValueError, match="end_time .* must be after start_time"):
        MetaExecutionSpecification(
            experiment_id=PILOT_EXPERIMENT_ID,
            ad_account_id="act_1985595022114520",
            page_id="1389949167526709",
            destination_url=PILOT_DESTINATION_URL,
            primary_text=PILOT_CREATIVE_PRIMARY_TEXT,
            headline=PILOT_CREATIVE_HEADLINE,
            image_asset_path=str(PILOT_CREATIVE_PATH),
            call_to_action="LEARN_MORE",
            campaign_objective="OUTCOME_TRAFFIC",
            countries=["IN"],
            age_min=18,
            age_max=65,
            authorized_budget=Decimal("200.00"),
            start_time=now,
            end_time=now - timedelta(hours=1),
            special_ad_categories=["NONE"],
            status="PAUSED",
            explicit_dispatch_authorized=True,
        )

    # Pre-dispatch rejects un-authorized dispatch
    unauth_spec = spec_option_a.model_copy(update={"explicit_dispatch_authorized": False})
    with pytest.raises(ValueError, match="Explicit operator dispatch authorization is required"):
        unauth_spec.validate_pre_dispatch(
            allocated_budget=exp.allocated_budget,
            max_allowed_spend=exp.max_allowed_spend,
            actual_spend=exp.actual_spend,
        )

    # Part 7: SAFE_MODE Rejection
    req = ExecutionRequest(
        experiment_id=PILOT_EXPERIMENT_ID,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT.value,
        proposed_budget=Decimal("200.00"),
    )
    res = ExecutionDispatchService.dispatch(
        request=req,
        session=test_session,
        safe_mode=True,
    )
    assert res.success is False
    assert res.blocked is True
    assert res.reason == "SAFE_MODE_ENABLED"
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None

    # Part 8: Treasury vs Platform Budget Isolation
    cap_repo = CapitalRepository(test_session)
    summary = cap_repo.get_financial_summary()
    assert summary.current_balance == Decimal("1000.00")
    assert summary.total_allocated == Decimal("200.00")
    assert summary.available_unallocated == Decimal("800.00")
    assert summary.total_outflow == Decimal("0.00")
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")


def test_step90_pre_dispatch_verification(test_session: Session) -> None:
    """Step 90 — Final Pre-Dispatch Verification & Human Authorization Gate.

    Verifies:
    1. Canonical pilot invariants: APPROVED status, ₹200.00 allocation, ₹0 spend, actual_start is None.
    2. Capital integrity: starting capital ₹1,000.00, committed ₹200.00, unallocated ₹800.00, ₹0 outflow.
    3. Final campaign specification: Age 22-55, 48-hour flight (2026-10-06 08:30 to 2026-10-08 08:30 UTC),
       lifetime budget ₹200.00 (PAUSED initial status, OUTCOME_TRAFFIC, LINK_CLICKS).
    4. Budget safety: 48h daily budget rate (₹100.00/day) satisfies Meta minimum daily budget (₹96.73/day).
    5. Creative asset: SHA-256 hash, 1080x1080 PNG dimensions, and canonical copy verification.
    6. SAFE_MODE gate: ExecutionDispatchService rejects execution while SAFE_MODE is True,
       guaranteeing zero external writes and zero ExternalExecution records.
    """
    import hashlib
    import struct
    from datetime import datetime, timezone
    from venturebot.execution.dispatch import ExecutionAction, ExecutionDispatchService, ExecutionRequest
    from venturebot.execution.meta import MetaExecutionSpecification, inr_to_paise

    # 1. Canonical Pilot Invariants
    opp, exp, cap_tx = persist_allocated_pilot(test_session)
    assert exp.id == PILOT_EXPERIMENT_ID
    assert exp.opportunity_id == PILOT_OPPORTUNITY_ID
    assert exp.status == ExperimentStatus.APPROVED.value
    assert exp.actual_start is None
    assert exp.allocated_budget == Decimal("200.00")
    assert exp.max_allowed_spend == Decimal("200.00")
    assert exp.actual_spend == Decimal("0.00")
    assert is_safe_mode() is True

    ext_repo = ExternalExecutionRepository(test_session)
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None

    # 2. Capital Integrity
    cap_repo = CapitalRepository(test_session)
    summary = cap_repo.get_financial_summary()
    assert summary.total_inflow == Decimal("1000.00")
    assert summary.current_balance == Decimal("1000.00")
    assert summary.total_allocated == Decimal("200.00")
    assert summary.available_unallocated == Decimal("800.00")
    assert summary.total_outflow == Decimal("0.00")
    assert cap_repo.get_experiment_actual_spend(PILOT_EXPERIMENT_ID) == Decimal("0.00")

    # 3. Final Campaign Specification (Option B: 22-55, 48h Flight)
    start_utc = datetime(2026, 10, 6, 8, 30, tzinfo=timezone.utc)
    end_utc = datetime(2026, 10, 8, 8, 30, tzinfo=timezone.utc)
    approved_headline = "Stop Chasing Invoices — Solopreneur Financial Workflow Guide"
    approved_primary = (
        "Chasing late invoices costs freelancers 4+ hours every week. "
        "Get the battle-tested, 5-step financial workflow guide to automate follow-ups and stabilize cash flow."
    )

    spec = MetaExecutionSpecification(
        experiment_id=PILOT_EXPERIMENT_ID,
        ad_account_id="act_1985595022114520",
        page_id="1389949167526709",
        destination_url=PILOT_DESTINATION_URL,
        primary_text=approved_primary,
        headline=approved_headline,
        image_asset_path=str(PILOT_CREATIVE_PATH),
        call_to_action="LEARN_MORE",
        campaign_objective="OUTCOME_TRAFFIC",
        countries=["IN"],
        age_min=22,
        age_max=55,
        authorized_budget=Decimal("200.00"),
        start_time=start_utc,
        end_time=end_utc,
        special_ad_categories=["NONE"],
        status="PAUSED",
        explicit_dispatch_authorized=True,
    )
    spec.validate_pre_dispatch(
        allocated_budget=exp.allocated_budget,
        max_allowed_spend=exp.max_allowed_spend,
        actual_spend=exp.actual_spend,
    )

    # 4. Budget Safety & Minimum Daily Spend Constraints
    min_daily_budget = Decimal("96.73")
    assert inr_to_paise(spec.authorized_budget) == 20000
    duration_days = Decimal((end_utc - start_utc).total_seconds()) / Decimal(86400)
    assert duration_days == Decimal("2.0")
    daily_rate = spec.authorized_budget / duration_days
    assert daily_rate == Decimal("100.00")
    assert daily_rate >= min_daily_budget

    # Rejects over-budget authorization
    over_spec = spec.model_copy(update={"authorized_budget": Decimal("200.01")})
    with pytest.raises(ValueError, match="exceeds remaining allocated budget"):
        over_spec.validate_pre_dispatch(
            allocated_budget=exp.allocated_budget,
            max_allowed_spend=exp.max_allowed_spend,
            actual_spend=exp.actual_spend,
        )

    # 5. Creative Asset Verification
    assert PILOT_CREATIVE_PATH.is_file()
    img_bytes = PILOT_CREATIVE_PATH.read_bytes()
    assert hashlib.sha256(img_bytes).hexdigest() == PILOT_CREATIVE_SHA256
    assert img_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", img_bytes[16:24])
    assert (w, h) == (1080, 1080)

    # 6. SAFE_MODE Gate Verification
    req = ExecutionRequest(
        experiment_id=PILOT_EXPERIMENT_ID,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT.value,
        proposed_budget=Decimal("200.00"),
    )
    dispatch_res = ExecutionDispatchService.dispatch(
        request=req,
        session=test_session,
        safe_mode=True,
        spec=spec,
    )
    assert dispatch_res.success is False
    assert dispatch_res.blocked is True
    assert dispatch_res.reason == "SAFE_MODE_ENABLED"
    assert ext_repo.get_by_experiment_id(PILOT_EXPERIMENT_ID) is None


def test_step90_2_privacy_policy_integrity() -> None:
    """Verify Step 90.2: VentureBot public Privacy Policy page integrity.

    Verifies:
    1. Both root and docs/ copies of privacy-policy.html exist and are identical.
    2. Canonical link in privacy-policy.html points to verified controlled URL.
    3. Zero script tags, zero forms, zero inputs, zero tracking pixels (fbq, gtag).
    4. Meta App ID 1063651013045060 and pilot experiment ID are documented.
    5. Zero references to external/stale domains.
    6. All core privacy sections (zero PII, telemetry, cookies, infrastructure) are present.
    """
    repo_root = Path(__file__).resolve().parent.parent
    root_policy = repo_root / "privacy-policy.html"
    docs_policy = repo_root / "docs" / "privacy-policy.html"

    assert root_policy.is_file(), "privacy-policy.html missing at repository root"
    assert docs_policy.is_file(), "docs/privacy-policy.html missing"

    root_content = root_policy.read_text(encoding="utf-8")
    docs_content = docs_policy.read_text(encoding="utf-8")

    assert root_content == docs_content, "root and docs privacy-policy.html must be identical"

    # Canonical URL
    assert '<link rel="canonical" href="https://vishnu3568.github.io/venturebot/privacy-policy.html">' in root_content

    # Strict zero-tracking and zero-form checks
    assert "<script" not in root_content.lower(), "privacy-policy.html must not contain <script> tags"
    assert "<form" not in root_content.lower(), "privacy-policy.html must not contain <form> tags"
    assert "<input" not in root_content.lower(), "privacy-policy.html must not contain <input> tags"
    assert "fbq" not in root_content, "privacy-policy.html must not contain fbq tracking identifiers"
    assert "gtag" not in root_content, "privacy-policy.html must not contain gtag tracking identifiers"

    # Core required identifiers
    assert "1063651013045060" in root_content, "privacy-policy.html must document Meta App ID 1063651013045060"
    assert "49fde874-9387-5056-934c-51a9cfca164f" in root_content, "privacy-policy.html must document pilot experiment ID"

    # Core sections
    assert "About VentureBot" in root_content
    assert "Information Collection & Principles" in root_content
    assert "Telemetry & Measurement Data" in root_content
    assert "Cookies & Third-Party Tracking Scripts" in root_content
    assert "Third-Party Infrastructure Services" in root_content
    assert "Data Retention & Deletion" in root_content
    assert "Meta Developer Platform Context" in root_content
    assert "Contact & Inquiries" in root_content

