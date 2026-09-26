"""Comprehensive test suite for Meta External Execution Reconciliation (Step 42).

Covers all 25 required test cases:
1. Campaign FOUND_EXACT → adopt, no POST
2. Campaign NOT_FOUND → POST allowed
3. Campaign AMBIGUOUS → no POST
4. Campaign UNKNOWN → no POST
5. Ad Set FOUND_EXACT → adopt, no POST
6. Ad Set NOT_FOUND → POST allowed
7. Ad Set AMBIGUOUS → no POST
8. Ad Set UNKNOWN → no POST
9. Creative FOUND_EXACT → adopt, no POST
10. Creative NOT_FOUND → POST allowed
11. Creative AMBIGUOUS → no POST
12. Creative UNKNOWN → no POST
13. Ad FOUND_EXACT → adopt, no POST
14. Ad NOT_FOUND → POST allowed
15. Ad AMBIGUOUS → no POST
16. Ad UNKNOWN → no POST
17. Lost campaign response recovery
18. Lost Ad Set response recovery
19. Lost Creative response recovery
20. Lost Ad response recovery
21. Parent mismatch is rejected
22. Existing persisted IDs are reused
23. Reconciliation does not mutate CapitalTransaction
24. SAFE_MODE still blocks before any write
25. Full deployment only reaches RUNNING after all five deployment tiers resolve successfully
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from typing import Any
import urllib.request
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.models import CapitalTransactionORM
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.execution.dispatch import (
    ExecutionAction,
    ExecutionDispatchService,
    ExecutionRequest,
    MetaExperimentDispatchService,
    _GatewayInvocationContext,
)
from venturebot.execution.meta import (
    LookupResult,
    MetaApiError,
    MetaApiTimeoutError,
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
    ReconciliationStatus,
    deterministic_ad_name,
    deterministic_adset_name,
    deterministic_campaign_name,
    deterministic_creative_name,
)
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.external_execution import ExternalExecution, ExternalExecutionStatus
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus
from venturebot.approval.service import ExperimentApprovalService

SAMPLE_ACCOUNT_ID = "act_1985595022114520"
SAMPLE_TOKEN = "EAABwzL12345fakeToken"
SAMPLE_PAGE_ID = "10987654321"


@pytest.fixture
def session() -> Session:
    """Provide isolated in-memory SQLite database session with starting capital initialized."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        cap_repo = CapitalRepository(sess)
        cap_repo.initialize_starting_capital()
        return sess


@pytest.fixture
def approved_experiment(session: Session) -> Experiment:
    """Provide a persisted approved experiment with linked opportunity."""
    opp_repo = OpportunityRepository(session)
    opp = opp_repo.create(
        Opportunity(
            title="SaaS Churn Reduction Guide",
            description="Playbook helping B2B SaaS founders decrease monthly subscriber churn.",
            category=OpportunityCategory.CONTENT,
            status=OpportunityStatus.DISCOVERED,
            source="Manual survey",
            audience="B2B SaaS founders in India",
            monetization_notes="Digital playbook at ₹499",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("2000.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
        )
    )

    exp_repo = ExperimentRepository(session)
    exp = exp_repo.create(
        Experiment(
            opportunity_id=opp.id,
            hypothesis="Targeting SaaS founders on Facebook will yield initial playbook buyers.",
            objective="Acquire 5 playbook downloads.",
            channel=Channel.FACEBOOK,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("100.00"),
            max_allowed_spend=Decimal("100.00"),
            actual_spend=Decimal("0.00"),
            success_criteria="5 sales achieved within ₹100 spend.",
            failure_criteria="Zero sales after ₹100 spend.",
            status=ExperimentStatus.DRAFT,
        )
    )

    app_res = ExperimentApprovalService.approve(
        session,
        exp.id,
        reason="Operator approved demand test.",
        allocated_budget=Decimal("100.00"),
    )
    assert app_res.is_approved is True
    assert app_res.experiment is not None
    return app_res.experiment


@pytest.fixture
def valid_spec(approved_experiment: Experiment) -> MetaExecutionSpecification:
    """Provide a valid execution specification for the approved experiment."""
    return MetaExecutionSpecification(
        experiment_id=approved_experiment.id,
        ad_account_id=SAMPLE_ACCOUNT_ID,
        page_id=SAMPLE_PAGE_ID,
        destination_url="https://example.com/playbook",
        primary_text="Stop subscriber churn today with our actionable B2B guide.",
        headline="Reduce Churn by 25%",
        authorized_budget=Decimal("50.00"),
        end_time=datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc),
        image_hash="img_hash_123456",
        explicit_dispatch_authorized=True,
    )


def make_reconciliation_transport(
    overrides: dict[str, Any] | None = None,
    default_empty: bool = True,
):
    """Create a mock transport that records requests and responds to Meta API queries."""
    recorded_requests: list[urllib.request.Request] = []
    custom = overrides or {}

    def _transport(req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        recorded_requests.append(req)
        url = req.full_url
        method = req.get_method()

        # Check explicit overrides first
        for pattern, handler in custom.items():
            if pattern in url:
                if isinstance(handler, Exception):
                    raise handler
                if callable(handler):
                    res = handler(req)
                    return res if isinstance(res, bytes) else str(res).encode("utf-8")
                return json.dumps(handler).encode("utf-8")

        # Account metadata GET
        if "/act_1985595022114520?fields=" in url and method == "GET":
            return json.dumps({
                "id": "act_1985595022114520",
                "name": "VentureBot Experiments",
                "account_status": 1,
                "currency": "INR",
            }).encode("utf-8")

        # Lookups (GET) default to empty data list if default_empty is True
        if method == "GET":
            if any(endpoint in url for endpoint in ("/campaigns?fields=", "/adsets?fields=", "/adcreatives?fields=", "/ads?fields=")):
                return json.dumps({"data": [] if default_empty else []}).encode("utf-8")

        # Creation mutations (POST)
        if method == "POST":
            if "/campaigns" in url and "act_" in url:
                return json.dumps({"id": "mock_camp_new"}).encode("utf-8")
            if "/adsets" in url and "act_" in url:
                return json.dumps({"id": "mock_adset_new"}).encode("utf-8")
            if "/adimages" in url and "act_" in url:
                return json.dumps({"images": {"test.png": {"hash": "mock_hash_new"}}}).encode("utf-8")
            if "/adcreatives" in url and "act_" in url:
                return json.dumps({"id": "mock_creative_new"}).encode("utf-8")
            if "/ads" in url and "act_" in url:
                return json.dumps({"id": "mock_ad_new"}).encode("utf-8")
            if "mock_camp_" in url or "camp_" in url:
                return json.dumps({"success": True}).encode("utf-8")

        return json.dumps({"data": []}).encode("utf-8")

    _transport.requests = recorded_requests  # type: ignore[attr-defined]
    return _transport


def _dispatch_via_gateway(
    session: Session,
    experiment_id: UUID,
    spec: MetaExecutionSpecification,
    adapter: MetaMarketingApiAdapter,
    safe_mode: bool = False,
):
    """Helper invoking ExecutionDispatchService.dispatch gateway."""
    req = ExecutionRequest(
        experiment_id=experiment_id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=spec.authorized_budget,
        metadata={"channel": "meta"},
    )
    return ExecutionDispatchService.dispatch(
        req,
        session=session,
        safe_mode=safe_mode,
        spec=spec,
        adapter=adapter,
    )


# ── 1-4: Campaign Reconciliation Tests ───────────────────────────────────────


def test_1_campaign_found_exact_adopts_and_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """1. Campaign FOUND_EXACT → adopt ID, no campaign POST."""
    c_name = deterministic_campaign_name(approved_experiment.id)
    transport = make_reconciliation_transport({
        "/campaigns?fields=": {
            "data": [{"id": "camp_existing_exact_1", "name": c_name, "status": "PAUSED"}]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.campaign_id == "camp_existing_exact_1"
    # Verify no POST to /campaigns
    for req in transport.requests:  # type: ignore[attr-defined]
        if "/campaigns" in req.full_url and req.get_method() == "POST":
            pytest.fail("Campaign POST was called when campaign was FOUND_EXACT.")


def test_2_campaign_not_found_creates_campaign(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """2. Campaign NOT_FOUND → POST allowed, newly created campaign ID adopted."""
    transport = make_reconciliation_transport({
        "/campaigns?fields=": {"data": []}
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.campaign_id == "mock_camp_new"
    # Verify POST to /campaigns was called exactly once
    post_count = sum(
        1 for req in transport.requests  # type: ignore[attr-defined]
        if "/campaigns" in req.full_url and req.get_method() == "POST"
    )
    assert post_count == 1


def test_3_campaign_ambiguous_stops_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """3. Campaign AMBIGUOUS (multiple matches) → STOP, no POST."""
    c_name = deterministic_campaign_name(approved_experiment.id)
    transport = make_reconciliation_transport({
        "/campaigns?fields=": {
            "data": [
                {"id": "camp_dup_1", "name": c_name, "status": "PAUSED"},
                {"id": "camp_dup_2", "name": c_name, "status": "PAUSED"},
            ]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    assert res.blocked is True
    assert "Ambiguous" in (res.reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.FAILED
    assert res.execution.campaign_id is None
    # Verify no POST of any kind was executed
    for req in transport.requests:  # type: ignore[attr-defined]
        assert req.get_method() != "POST", f"POST called: {req.full_url}"


def test_4_campaign_unknown_stops_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """4. Campaign UNKNOWN (timeout/network error) → STOP, no POST."""
    transport = make_reconciliation_transport({
        "/campaigns?fields=": TimeoutError("Meta Graph API connection timed out.")
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    assert res.blocked is True
    assert "UNKNOWN" in (res.reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.TIMEOUT
    # Zero POST requests
    for req in transport.requests:  # type: ignore[attr-defined]
        assert req.get_method() != "POST"


# ── 5-8: Ad Set Reconciliation Tests ─────────────────────────────────────────


def test_5_adset_found_exact_adopts_and_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """5. Ad Set FOUND_EXACT → adopt ID, no adset POST."""
    as_name = deterministic_adset_name(approved_experiment.id)
    transport = make_reconciliation_transport({
        "/adsets?fields=": {
            "data": [{
                "id": "adset_existing_exact_1",
                "name": as_name,
                "status": "PAUSED",
                "campaign_id": "mock_camp_new",
                "lifetime_budget": "5000",
                "end_time": "2026-10-01T12:00:00+0000",
            }]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.adset_id == "adset_existing_exact_1"
    # Ensure adset POST was not executed
    for req in transport.requests:  # type: ignore[attr-defined]
        if "/adsets" in req.full_url and req.get_method() == "POST":
            pytest.fail("Ad Set POST was called when ad set was FOUND_EXACT.")


def test_6_adset_not_found_creates_adset(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """6. Ad Set NOT_FOUND → POST allowed, newly created ad set adopted."""
    transport = make_reconciliation_transport({
        "/adsets?fields=": {"data": []}
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.adset_id == "mock_adset_new"
    adset_posts = [
        req for req in transport.requests  # type: ignore[attr-defined]
        if "/adsets" in req.full_url and req.get_method() == "POST"
    ]
    assert len(adset_posts) == 1


def test_7_adset_ambiguous_stops_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """7. Ad Set AMBIGUOUS (multiple matches under parent campaign) → STOP, no POST."""
    as_name = deterministic_adset_name(approved_experiment.id)
    transport = make_reconciliation_transport({
        "/adsets?fields=": {
            "data": [
                {"id": "as_1", "name": as_name, "status": "PAUSED", "campaign_id": "mock_camp_new"},
                {"id": "as_2", "name": as_name, "status": "PAUSED", "campaign_id": "mock_camp_new"},
            ]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    assert res.blocked is True
    assert "Ambiguous" in (res.reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.FAILED
    assert res.execution.adset_id is None
    # Ad set POST must not be called
    for req in transport.requests:  # type: ignore[attr-defined]
        assert not ("/adsets" in req.full_url and req.get_method() == "POST")


def test_8_adset_unknown_stops_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """8. Ad Set UNKNOWN (HTTP 500 / network error) → STOP, no POST."""
    transport = make_reconciliation_transport({
        "/adsets?fields=": MetaApiError("HTTP 500 Internal Server Error from Meta")
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    assert res.blocked is True
    assert "UNKNOWN" in (res.reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.FAILED
    assert res.execution.adset_id is None
    # No ad set POST
    for req in transport.requests:  # type: ignore[attr-defined]
        assert not ("/adsets" in req.full_url and req.get_method() == "POST")


# ── 9-12: Creative Reconciliation Tests ──────────────────────────────────────


def test_9_creative_found_exact_adopts_and_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """9. Creative FOUND_EXACT → adopt ID, no creative POST."""
    cr_name = deterministic_creative_name(approved_experiment.id)
    transport = make_reconciliation_transport({
        "/adcreatives?fields=": {
            "data": [{
                "id": "creative_existing_exact_1",
                "name": cr_name,
                "object_story_spec": {
                    "page_id": SAMPLE_PAGE_ID,
                    "link_data": {
                        "link": valid_spec.destination_url,
                        "image_hash": valid_spec.image_hash,
                        "message": valid_spec.primary_text,
                        "name": valid_spec.headline,
                    },
                },
            }]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.creative_id == "creative_existing_exact_1"
    # Ensure creative POST was not called
    for req in transport.requests:  # type: ignore[attr-defined]
        if "/adcreatives" in req.full_url and req.get_method() == "POST":
            pytest.fail("Creative POST was called when creative was FOUND_EXACT.")


def test_10_creative_not_found_creates_creative(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """10. Creative NOT_FOUND → POST allowed, newly created creative adopted."""
    transport = make_reconciliation_transport({
        "/adcreatives?fields=": {"data": []}
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.creative_id == "mock_creative_new"
    cr_posts = [
        req for req in transport.requests  # type: ignore[attr-defined]
        if "/adcreatives" in req.full_url and req.get_method() == "POST"
    ]
    assert len(cr_posts) == 1


def test_11_creative_ambiguous_stops_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """11. Creative AMBIGUOUS (multiple exact matches) → STOP, no POST."""
    cr_name = deterministic_creative_name(approved_experiment.id)
    matching_spec = {
        "page_id": SAMPLE_PAGE_ID,
        "link_data": {
            "link": valid_spec.destination_url,
            "image_hash": valid_spec.image_hash,
        },
    }
    transport = make_reconciliation_transport({
        "/adcreatives?fields=": {
            "data": [
                {"id": "cr_1", "name": cr_name, "object_story_spec": matching_spec},
                {"id": "cr_2", "name": cr_name, "object_story_spec": matching_spec},
            ]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    assert res.blocked is True
    assert "Ambiguous" in (res.reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.FAILED
    assert res.execution.creative_id is None
    # No creative POST
    for req in transport.requests:  # type: ignore[attr-defined]
        assert not ("/adcreatives" in req.full_url and req.get_method() == "POST")


def test_12_creative_unknown_stops_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """12. Creative UNKNOWN (timeout/auth error) → STOP, no POST."""
    transport = make_reconciliation_transport({
        "/adcreatives?fields=": MetaApiTimeoutError("Creative query timed out")
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    assert res.blocked is True
    assert "UNKNOWN" in (res.reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.TIMEOUT
    assert res.execution.creative_id is None
    for req in transport.requests:  # type: ignore[attr-defined]
        assert not ("/adcreatives" in req.full_url and req.get_method() == "POST")


# ── 13-16: Ad Reconciliation Tests ──────────────────────────────────────────


def test_13_ad_found_exact_adopts_and_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """13. Ad FOUND_EXACT → adopt ID, no ad POST."""
    ad_name = deterministic_ad_name(approved_experiment.id)
    transport = make_reconciliation_transport({
        "/ads?fields=": {
            "data": [{
                "id": "ad_existing_exact_1",
                "name": ad_name,
                "status": "PAUSED",
                "adset_id": "mock_adset_new",
                "creative": {"id": "mock_creative_new"},
            }]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.ad_id == "ad_existing_exact_1"
    # Ensure ad POST was not called
    for req in transport.requests:  # type: ignore[attr-defined]
        if "/ads" in req.full_url and "/adsets" not in req.full_url and req.get_method() == "POST":
            pytest.fail("Ad POST was called when ad was FOUND_EXACT.")


def test_14_ad_not_found_creates_ad(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """14. Ad NOT_FOUND → POST allowed, newly created ad adopted."""
    transport = make_reconciliation_transport({
        "/ads?fields=": {"data": []}
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.ad_id == "mock_ad_new"
    ad_posts = [
        req for req in transport.requests  # type: ignore[attr-defined]
        if "/ads" in req.full_url and "/adsets" not in req.full_url and req.get_method() == "POST"
    ]
    assert len(ad_posts) == 1


def test_15_ad_ambiguous_stops_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """15. Ad AMBIGUOUS (multiple matching ads under parent adset) → STOP, no POST."""
    ad_name = deterministic_ad_name(approved_experiment.id)
    transport = make_reconciliation_transport({
        "/ads?fields=": {
            "data": [
                {"id": "ad_1", "name": ad_name, "status": "PAUSED", "adset_id": "mock_adset_new", "creative": {"id": "mock_creative_new"}},
                {"id": "ad_2", "name": ad_name, "status": "PAUSED", "adset_id": "mock_adset_new", "creative": {"id": "mock_creative_new"}},
            ]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    assert res.blocked is True
    assert "Ambiguous" in (res.reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.FAILED
    assert res.execution.ad_id is None
    # No ad POST
    for req in transport.requests:  # type: ignore[attr-defined]
        assert not ("/ads" in req.full_url and "/adsets" not in req.full_url and req.get_method() == "POST")


def test_16_ad_unknown_stops_no_post(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """16. Ad UNKNOWN (malformed JSON / timeout) → STOP, no POST."""
    transport = make_reconciliation_transport({
        "/ads?fields=": "NOT_VALID_JSON_RESPONSE"
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    assert res.blocked is True
    assert "UNKNOWN" in (res.reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.FAILED
    assert res.execution.ad_id is None
    for req in transport.requests:  # type: ignore[attr-defined]
        assert not ("/ads" in req.full_url and "/adsets" not in req.full_url and req.get_method() == "POST")


# ── 17-20: Lost Response Recovery Tests ──────────────────────────────────────


def test_17_lost_campaign_response_recovery(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """17. Campaign POST was executed remotely, but response was lost (local execution record has no campaign_id).

    Next attempt:
    - performs campaign lookup
    - finds exact matching remote campaign
    - adopts it without issuing a second campaign POST!
    """
    c_name = deterministic_campaign_name(approved_experiment.id)

    # Remote has the campaign from the previous lost-response POST
    transport = make_reconciliation_transport({
        "/campaigns?fields=": {
            "data": [{"id": "camp_from_lost_response", "name": c_name, "status": "PAUSED"}]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.campaign_id == "camp_from_lost_response"

    # CRITICAL: zero campaign POSTs
    campaign_posts = [
        req for req in transport.requests  # type: ignore[attr-defined]
        if "/campaigns" in req.full_url and req.get_method() == "POST"
    ]
    assert len(campaign_posts) == 0


def test_18_lost_adset_response_recovery(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """18. Ad Set POST was executed remotely, but response was lost (execution has campaign_id, but no adset_id).

    Next attempt:
    - reuses persisted campaign_id
    - reconciles ad set
    - finds exact matching remote ad set
    - adopts it without issuing a second ad set POST!
    """
    as_name = deterministic_adset_name(approved_experiment.id)
    exec_repo = ExternalExecutionRepository(session)
    exec_repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id="camp_persisted_prior",
            status=ExternalExecutionStatus.PARTIAL_CAMPAIGN,
        )
    )

    transport = make_reconciliation_transport({
        "/adsets?fields=": {
            "data": [{
                "id": "adset_from_lost_response",
                "name": as_name,
                "status": "PAUSED",
                "campaign_id": "camp_persisted_prior",
                "lifetime_budget": "5000",
                "end_time": "2026-10-01T12:00:00+0000",
            }]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.campaign_id == "camp_persisted_prior"
    assert res.execution.adset_id == "adset_from_lost_response"

    # CRITICAL: zero ad set POSTs
    adset_posts = [
        req for req in transport.requests  # type: ignore[attr-defined]
        if "/adsets" in req.full_url and req.get_method() == "POST"
    ]
    assert len(adset_posts) == 0


def test_19_lost_creative_response_recovery(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """19. Creative POST was executed remotely, response lost.

    Next attempt reconciles creative, finds exact match, adopts it without duplicate POST.
    """
    cr_name = deterministic_creative_name(approved_experiment.id)
    exec_repo = ExternalExecutionRepository(session)
    exec_repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id="camp_persisted_prior",
            adset_id="adset_persisted_prior",
            image_hash="img_hash_123456",
            status=ExternalExecutionStatus.PARTIAL_ADSET,
        )
    )

    transport = make_reconciliation_transport({
        "/adcreatives?fields=": {
            "data": [{
                "id": "creative_from_lost_response",
                "name": cr_name,
                "object_story_spec": {
                    "page_id": SAMPLE_PAGE_ID,
                    "link_data": {
                        "link": valid_spec.destination_url,
                        "image_hash": "img_hash_123456",
                    },
                },
            }]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.creative_id == "creative_from_lost_response"

    cr_posts = [
        req for req in transport.requests  # type: ignore[attr-defined]
        if "/adcreatives" in req.full_url and req.get_method() == "POST"
    ]
    assert len(cr_posts) == 0


def test_20_lost_ad_response_recovery(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """20. Ad POST was executed remotely, response lost.

    Next attempt reconciles ad, finds exact match under ad set and creative, adopts it, reaches DEPLOYED.
    """
    ad_name = deterministic_ad_name(approved_experiment.id)
    exec_repo = ExternalExecutionRepository(session)
    exec_repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id="camp_persisted_prior",
            adset_id="adset_persisted_prior",
            image_hash="img_hash_123456",
            creative_id="creative_persisted_prior",
            status=ExternalExecutionStatus.PARTIAL_CREATIVE,
        )
    )

    transport = make_reconciliation_transport({
        "/ads?fields=": {
            "data": [{
                "id": "ad_from_lost_response",
                "name": ad_name,
                "status": "PAUSED",
                "adset_id": "adset_persisted_prior",
                "creative": {"id": "creative_persisted_prior"},
            }]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.ad_id == "ad_from_lost_response"
    assert res.execution.status == ExternalExecutionStatus.DEPLOYED

    ad_posts = [
        req for req in transport.requests  # type: ignore[attr-defined]
        if "/ads" in req.full_url and "/adsets" not in req.full_url and req.get_method() == "POST"
    ]
    assert len(ad_posts) == 0


# ── 21-25: Integrity, Invariant, and Guardrail Tests ─────────────────────────


def test_21_parent_mismatch_is_rejected(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """21. Parent mismatch is rejected: adset with same name but belonging to another campaign is NOT adopted."""
    as_name = deterministic_adset_name(approved_experiment.id)
    transport = make_reconciliation_transport({
        "/adsets?fields=": {
            "data": [{
                "id": "adset_from_wrong_campaign",
                "name": as_name,
                "status": "PAUSED",
                "campaign_id": "different_campaign_999",  # WRONG PARENT
                "lifetime_budget": "5000",
            }]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    # Lookup ignores the wrong parent match, treats as NOT_FOUND under our campaign, and creates a fresh adset
    assert res.success is True
    assert res.execution is not None
    assert res.execution.adset_id == "mock_adset_new"
    assert res.execution.adset_id != "adset_from_wrong_campaign"


def test_22_existing_persisted_ids_are_reused(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """22. Existing persisted IDs are reused without querying remote lookup."""
    exec_repo = ExternalExecutionRepository(session)
    exec_repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id="camp_already_known",
            adset_id="adset_already_known",
            image_hash="img_hash_123456",
            status=ExternalExecutionStatus.PARTIAL_ADSET,
        )
    )

    transport = make_reconciliation_transport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is True
    assert res.execution is not None
    assert res.execution.campaign_id == "camp_already_known"
    assert res.execution.adset_id == "adset_already_known"

    # Campaign and ad set lookup GET requests were NOT made
    for req in transport.requests:  # type: ignore[attr-defined]
        assert "/campaigns?fields=" not in req.full_url
        assert "/adsets?fields=" not in req.full_url


def test_23_reconciliation_does_not_mutate_capital_transaction(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """23. Reconciliation MUST have zero CapitalTransaction mutations."""
    # Check baseline capital transactions (only the starting capital initial entry)
    initial_txs = session.query(CapitalTransactionORM).all()
    initial_count = len(initial_txs)
    initial_liquid = CapitalRepository(session).get_current_balance()

    transport = make_reconciliation_transport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.success is True

    # Post-dispatch verification
    post_txs = session.query(CapitalTransactionORM).all()
    assert len(post_txs) == initial_count
    post_liquid = CapitalRepository(session).get_current_balance()
    assert post_liquid == initial_liquid == Decimal("1000.00")


def test_24_safe_mode_still_blocks_before_any_write(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """24. SAFE_MODE=True still strictly blocks before any external write or Tier 2 dispatch."""
    transport = make_reconciliation_transport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter, safe_mode=True)

    assert res.success is False
    assert res.blocked is True
    assert res.reason == "SAFE_MODE_ENABLED"

    # Zero transport calls whatsoever
    assert len(transport.requests) == 0  # type: ignore[attr-defined]


def test_25_full_deployment_only_reaches_running_after_all_tiers_deployed(
    session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification
):
    """25. Internal experiment only transitions to RUNNING after all 5 tiers deploy.

    If an intermediate tier (e.g. ad creation) fails, experiment remains APPROVED, never RUNNING.
    """
    transport = make_reconciliation_transport({
        "/ads?fields=": MetaApiError("Ad creation blocked due to validation failure.")
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter)

    assert res.success is False
    # Verify experiment in DB is still APPROVED, NOT RUNNING
    exp_db = ExperimentRepository(session).get(approved_experiment.id)
    assert exp_db is not None
    assert exp_db.status == ExperimentStatus.APPROVED

    # Now fix the issue so all tiers deploy
    transport_success = make_reconciliation_transport()
    adapter_success = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_success)

    res_success = _dispatch_via_gateway(session, approved_experiment.id, valid_spec, adapter=adapter_success)
    assert res_success.success is True

    # Now experiment MUST be RUNNING
    exp_db_running = ExperimentRepository(session).get(approved_experiment.id)
    assert exp_db_running is not None
    assert exp_db_running.status == ExperimentStatus.RUNNING


# ── Unit Tests: Deterministic Naming Helpers ─────────────────────────────────


def test_deterministic_helpers_format():
    """Verify deterministic helpers construct valid names matching locked contracts."""
    test_id = UUID("12345678-1234-5678-1234-567812345678")
    assert deterministic_campaign_name(test_id) == "VB-EXP-12345678-1234-5678-1234-567812345678"
    assert deterministic_adset_name(test_id) == "VB-EXP-12345678-1234-5678-1234-567812345678-ADSET"
    assert deterministic_creative_name(test_id) == "VB-EXP-12345678-1234-5678-1234-567812345678-CREATIVE"
    assert deterministic_ad_name(test_id) == "VB-EXP-12345678-1234-5678-1234-567812345678-AD"


def test_deterministic_helpers_type_enforcement():
    """Verify deterministic helpers reject non-UUID inputs."""
    with pytest.raises(TypeError):
        deterministic_adset_name("not-a-uuid")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        deterministic_creative_name("not-a-uuid")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        deterministic_ad_name("not-a-uuid")  # type: ignore[arg-type]


# ── Unit Tests: Adapter Direct Lookup Methods ────────────────────────────────


def test_adapter_lookup_campaign_unit():
    """Unit tests for MetaMarketingApiAdapter.lookup_campaign."""
    def _transport(req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        if "TIMEOUT" in req.full_url:
            raise MetaApiTimeoutError("Timeout")
        if "ERROR" in req.full_url:
            raise MetaApiError("API Error")
        if "MALFORMED" in req.full_url:
            return b'{"not_data": 123}'
        if "EMPTY" in req.full_url:
            return b'{"data": []}'
        if "EXACT" in req.full_url:
            return b'{"data": [{"id": "camp_exact", "name": "VB-EXP-EXACT", "status": "PAUSED"}]}'
        if "AMBIGUOUS" in req.full_url:
            return b'{"data": [{"id": "c1", "name": "VB-EXP-AMBIGUOUS"}, {"id": "c2", "name": "VB-EXP-AMBIGUOUS"}]}'
        return b'{"data": []}'

    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=_transport)

    assert adapter.lookup_campaign("VB-EXP-EMPTY").status == ReconciliationStatus.NOT_FOUND
    exact = adapter.lookup_campaign("VB-EXP-EXACT")
    assert exact.status == ReconciliationStatus.FOUND_EXACT
    assert exact.resource_id == "camp_exact"

    amb = adapter.lookup_campaign("VB-EXP-AMBIGUOUS")
    assert amb.status == ReconciliationStatus.AMBIGUOUS

    assert adapter.lookup_campaign("VB-EXP-TIMEOUT").status == ReconciliationStatus.UNKNOWN
    assert adapter.lookup_campaign("VB-EXP-ERROR").status == ReconciliationStatus.UNKNOWN
    assert adapter.lookup_campaign("VB-EXP-MALFORMED").status == ReconciliationStatus.UNKNOWN


def test_adapter_lookup_adset_unit():
    """Unit tests for MetaMarketingApiAdapter.lookup_adset."""
    def _transport(req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        if "TIMEOUT" in req.full_url:
            raise MetaApiTimeoutError("Timeout")
        return json.dumps({
            "data": [{
                "id": "as_100",
                "name": "VB-EXP-TEST-ADSET",
                "campaign_id": "c_correct",
                "lifetime_budget": "5000",
            }]
        }).encode("utf-8")

    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=_transport)

    # Correct parent and budget -> FOUND_EXACT
    res = adapter.lookup_adset(
        campaign_id="c_correct",
        adset_name="VB-EXP-TEST-ADSET",
        expected_lifetime_budget_paise=5000,
    )
    assert res.status == ReconciliationStatus.FOUND_EXACT
    assert res.resource_id == "as_100"

    # Wrong parent -> NOT_FOUND (parent rejected)
    res_wrong_parent = adapter.lookup_adset(
        campaign_id="c_other",
        adset_name="VB-EXP-TEST-ADSET",
    )
    assert res_wrong_parent.status == ReconciliationStatus.NOT_FOUND

    # Timeout -> UNKNOWN
    res_to = adapter.lookup_adset(campaign_id="TIMEOUT", adset_name="VB-EXP-TEST-ADSET")
    assert res_to.status == ReconciliationStatus.UNKNOWN


def test_adapter_lookup_creative_unit():
    """Unit tests for MetaMarketingApiAdapter.lookup_creative."""
    def _transport(req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        return json.dumps({
            "data": [{
                "id": "cr_100",
                "name": "VB-EXP-TEST-CREATIVE",
                "object_story_spec": {
                    "page_id": "111",
                    "link_data": {
                        "link": "https://example.com",
                        "image_hash": "hash_123",
                    },
                },
            }]
        }).encode("utf-8")

    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=_transport)

    # Exact match on all 4 criteria
    res = adapter.lookup_creative(
        creative_name="VB-EXP-TEST-CREATIVE",
        page_id="111",
        image_hash="hash_123",
        destination_url="https://example.com",
    )
    assert res.status == ReconciliationStatus.FOUND_EXACT
    assert res.resource_id == "cr_100"

    # Mismatch on image_hash -> NOT_FOUND
    res_wrong_hash = adapter.lookup_creative(
        creative_name="VB-EXP-TEST-CREATIVE",
        page_id="111",
        image_hash="wrong_hash",
        destination_url="https://example.com",
    )
    assert res_wrong_hash.status == ReconciliationStatus.NOT_FOUND


def test_adapter_lookup_ad_unit():
    """Unit tests for MetaMarketingApiAdapter.lookup_ad."""
    def _transport(req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        return json.dumps({
            "data": [{
                "id": "ad_100",
                "name": "VB-EXP-TEST-AD",
                "adset_id": "as_111",
                "creative": {"id": "cr_222"},
            }]
        }).encode("utf-8")

    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=_transport)

    # Exact match on name, adset, and creative
    res = adapter.lookup_ad(adset_id="as_111", ad_name="VB-EXP-TEST-AD", creative_id="cr_222")
    assert res.status == ReconciliationStatus.FOUND_EXACT
    assert res.resource_id == "ad_100"

    # Mismatch on creative -> NOT_FOUND
    res_wrong_cr = adapter.lookup_ad(adset_id="as_111", ad_name="VB-EXP-TEST-AD", creative_id="cr_wrong")
    assert res_wrong_cr.status == ReconciliationStatus.NOT_FOUND

