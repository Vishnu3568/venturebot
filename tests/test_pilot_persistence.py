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
from venturebot.models.capital import TransactionType
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

