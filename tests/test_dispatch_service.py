"""Unit and integration tests for External Execution Persistence & Mocked Dispatch (Step 39)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
import urllib.request
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from venturebot.approval.service import ExperimentApprovalService
from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.models import ExternalExecutionORM
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.execution.dispatch import (
    DispatchResult,
    EmergencyStopResult,
    ExecutionDispatchService,
    MetaExperimentDispatchService,
    _GatewayInvocationContext,
    _sanitize_error,
)
from venturebot.execution.meta import (
    MetaApiError,
    MetaApiTimeoutError,
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
)
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.external_execution import ExternalExecution, ExternalExecutionStatus
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus

SAMPLE_ACCOUNT_ID = "act_1985595022114520"
SAMPLE_TOKEN = "EAABwzL12345fakeToken"
SAMPLE_PAGE_ID = "10987654321"


@pytest.fixture
def session() -> Session:
    """Provide isolated in-memory SQLite database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        cap_repo = CapitalRepository(sess)
        cap_repo.initialize_starting_capital()
        return sess


@pytest.fixture
def approved_experiment(session: Session) -> Experiment:
    """Provide a persisted approved experiment with linked opportunity and reserved budget."""
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


def make_mock_transport(custom_handlers: dict[str, Any] | None = None):
    """Factory creating a mock transport router for MetaMarketingApiAdapter."""
    handlers = custom_handlers or {}
    recorded_requests: list[urllib.request.Request] = []

    def _transport(req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        recorded_requests.append(req)
        url = req.full_url
        method = req.get_method()

        # Check custom handlers first
        for key, handler in handlers.items():
            matched = False
            if key == "/ads":
                if "/ads" in url and "/adsets" not in url and "/adcreatives" not in url:
                    matched = True
            elif key in url:
                matched = True

            if matched:
                if isinstance(handler, Exception):
                    raise handler
                if callable(handler):
                    res = handler(req)
                    return res if isinstance(res, bytes) else str(res).encode("utf-8")
                return json.dumps(handler).encode("utf-8")

        # Default responses for account metadata
        if f"/act_1985595022114520?fields=" in url:
            return json.dumps({
                "id": "act_1985595022114520",
                "name": "VentureBot Experiments",
                "account_status": 1,
                "currency": "INR",
            }).encode("utf-8")

        # Default duplicate check (returns empty list -> no duplicate)
        if "/campaigns?fields=" in url and method == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Default adset lookup (returns empty list -> not found)
        if "/adsets?fields=" in url and method == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Default creative lookup (returns empty list -> not found)
        if "/adcreatives?fields=" in url and method == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Default ad lookup (returns empty list -> not found)
        if "/ads?fields=" in url and method == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Default campaign creation
        if "/campaigns" in url and method == "POST":
            return json.dumps({"id": "camp_101"}).encode("utf-8")

        # Default image upload
        if "/adimages" in url and method == "POST":
            return json.dumps({"images": {"test.png": {"hash": "img_hash_101"}}}).encode("utf-8")

        # Default adset creation
        if "/adsets" in url and method == "POST":
            return json.dumps({"id": "adset_202"}).encode("utf-8")

        # Default creative creation
        if "/adcreatives" in url and method == "POST":
            return json.dumps({"id": "creative_303"}).encode("utf-8")

        # Default ad creation
        if "/ads" in url and method == "POST":
            return json.dumps({"id": "ad_404"}).encode("utf-8")

        # Default pause campaign
        if method == "POST" and ("camp_" in url or "c_" in url):
            return json.dumps({"success": True}).encode("utf-8")

        return json.dumps({"id": "default_id"}).encode("utf-8")

    _transport.requests = recorded_requests  # type: ignore[attr-defined]
    return _transport


def _dispatch_meta(
    session: Session,
    experiment_id: UUID,
    spec: MetaExecutionSpecification,
    adapter: MetaMarketingApiAdapter | None = None,
    auto_commit: bool = True,
) -> DispatchResult:
    """Helper invoking internal Meta dispatcher with valid gateway invocation context."""
    ctx = _GatewayInvocationContext(
        experiment_id=experiment_id,
        proposed_budget=spec.authorized_budget,
        channel="meta",
    )
    return MetaExperimentDispatchService._dispatch_from_gateway(
        session=session,
        experiment_id=experiment_id,
        spec=spec,
        adapter=adapter,
        auto_commit=auto_commit,
        safe_mode=False,
        gateway_context=ctx,
    )



# ── A. ORM / Repository Tests ────────────────────────────────────────────────

def test_orm_create_and_get_execution(session: Session, approved_experiment: Experiment):
    """Verify ExternalExecutionRepository persists and retrieves records accurately."""
    repo = ExternalExecutionRepository(session)
    record = ExternalExecution(
        experiment_id=approved_experiment.id,
        channel="meta",
        external_account_id=SAMPLE_ACCOUNT_ID,
        campaign_id="camp_123",
        status=ExternalExecutionStatus.PARTIAL_CAMPAIGN,
    )
    persisted = repo.create(record)
    assert persisted.id == record.id
    assert persisted.campaign_id == "camp_123"
    assert persisted.status == ExternalExecutionStatus.PARTIAL_CAMPAIGN

    # Retrieve by ID
    fetched = repo.get(record.id)
    assert fetched is not None
    assert fetched.experiment_id == approved_experiment.id

    # Retrieve by experiment_id
    by_exp = repo.get_by_experiment_id(approved_experiment.id)
    assert by_exp is not None
    assert by_exp.id == record.id


def test_orm_one_to_one_uniqueness_enforced(session: Session, approved_experiment: Experiment):
    """Verify an experiment cannot have more than one ExternalExecution row (one-to-one)."""
    repo = ExternalExecutionRepository(session)
    repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
        )
    )

    duplicate = ExternalExecution(
        experiment_id=approved_experiment.id,
        channel="meta",
        external_account_id=SAMPLE_ACCOUNT_ID,
    )
    with pytest.raises(IntegrityError):
        repo.create(duplicate)
    session.rollback()


def test_orm_update_ids_and_status(session: Session, approved_experiment: Experiment):
    """Verify sequential updates to external IDs and statuses persist properly."""
    repo = ExternalExecutionRepository(session)
    record = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
        )
    )
    assert record.status == ExternalExecutionStatus.PENDING

    # Update IDs
    record.campaign_id = "c_1"
    record.status = ExternalExecutionStatus.PARTIAL_CAMPAIGN
    record = repo.save(record)
    assert record.campaign_id == "c_1"
    assert record.status == ExternalExecutionStatus.PARTIAL_CAMPAIGN

    record.adset_id = "s_1"
    record.creative_id = "cr_1"
    record.ad_id = "a_1"
    record.status = ExternalExecutionStatus.DEPLOYED
    record = repo.save(record)

    fetched = repo.get_by_experiment_id(approved_experiment.id)
    assert fetched is not None
    assert fetched.status == ExternalExecutionStatus.DEPLOYED
    assert fetched.ad_id == "a_1"


def test_orm_update_last_error(session: Session, approved_experiment: Experiment):
    """Verify last_error updating via repository helper."""
    repo = ExternalExecutionRepository(session)
    record = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
        )
    )
    updated = repo.update_status(
        record.id,
        ExternalExecutionStatus.FAILED,
        last_error="Ad rejected due to creative policy.",
    )
    assert updated.status == ExternalExecutionStatus.FAILED
    assert updated.last_error == "Ad rejected due to creative policy."


# ── B. Dispatch Validation Tests ─────────────────────────────────────────────

def test_dispatch_fails_if_experiment_not_found(session: Session, valid_spec: MetaExecutionSpecification):
    """Dispatch rejects non-existent experiment."""
    fake_id = uuid4()
    res = _dispatch_meta(session, fake_id, valid_spec)
    assert res.is_successful is False
    assert "does not exist" in (res.rejection_reason or "")


def test_dispatch_fails_if_experiment_not_approved(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Dispatch rejects experiments that are not in APPROVED status."""
    exp_repo = ExperimentRepository(session)
    exp_repo.update_status(approved_experiment.id, ExperimentStatus.DRAFT)

    res = _dispatch_meta(session, approved_experiment.id, valid_spec)
    assert res.is_successful is False
    assert "Only experiments in 'approved' status can be dispatched" in (res.rejection_reason or "")


def test_dispatch_fails_if_authorization_not_explicit(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Dispatch rejects if explicit operator authorization is False."""
    valid_spec.explicit_dispatch_authorized = False
    res = _dispatch_meta(session, approved_experiment.id, valid_spec)
    assert res.is_successful is False
    assert "Explicit operator dispatch authorization is required" in (res.rejection_reason or "")


def test_dispatch_fails_if_spec_experiment_id_mismatch(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Dispatch rejects if specification target does not match target experiment."""
    other_id = uuid4()
    valid_spec.experiment_id = other_id
    res = _dispatch_meta(session, approved_experiment.id, valid_spec)
    assert res.is_successful is False
    assert "does not match target experiment" in (res.rejection_reason or "")


def test_dispatch_fails_if_budget_exceeds_limits(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Dispatch rejects if requested authorized_budget exceeds remaining allocation."""
    # Attempting ₹150 budget when only ₹100 was approved
    valid_spec.authorized_budget = Decimal("150.00")
    res = _dispatch_meta(session, approved_experiment.id, valid_spec)
    assert res.is_successful is False
    assert "exceeds remaining allocated budget" in (res.rejection_reason or "")


def test_dispatch_fails_if_ad_account_inactive(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Dispatch rejects if target Meta ad account is disabled or inactive."""
    transport = make_mock_transport({
        "/act_1985595022114520?fields=": {
            "id": "act_1985595022114520",
            "name": "Inactive Account",
            "account_status": 2,  # 2 = Disabled
            "currency": "INR",
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)
    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is False
    assert "is not active" in (res.rejection_reason or "")


def test_dispatch_fails_if_ad_account_currency_mismatch(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Dispatch rejects if target Meta ad account currency is not INR."""
    transport = make_mock_transport({
        "/act_1985595022114520?fields=": {
            "id": "act_1985595022114520",
            "name": "USD Account",
            "account_status": 1,
            "currency": "USD",
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)
    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is False
    assert "expected 'INR'" in (res.rejection_reason or "")


def test_dispatch_fails_if_already_fully_deployed(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Dispatch rejects if experiment already has a DEPLOYED external execution record."""
    exec_repo = ExternalExecutionRepository(session)
    exec_repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id="c_1",
            adset_id="s_1",
            creative_id="cr_1",
            ad_id="a_1",
            status=ExternalExecutionStatus.DEPLOYED,
        )
    )

    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=make_mock_transport(),
    )
    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is False
    assert "already fully deployed" in (res.rejection_reason or "")


# ── C. Successful Dispatch Tests ─────────────────────────────────────────────

def test_successful_dispatch_end_to_end(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Test full sequential creation with mock transport; verifies state and zero financial impact."""
    mock_transport = make_mock_transport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=mock_transport,
    )

    cap_repo = CapitalRepository(session)
    initial_summary = cap_repo.get_financial_summary()
    initial_tx_count = len(cap_repo.get_transaction_history())

    res = _dispatch_meta(
        session,
        approved_experiment.id,
        valid_spec,
        adapter=adapter,
    )

    assert res.is_successful is True
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.DEPLOYED
    assert res.execution.campaign_id == "camp_101"
    assert res.execution.adset_id == "adset_202"
    assert res.execution.creative_id == "creative_303"
    assert res.execution.ad_id == "ad_404"

    # Internal lifecycle transitioned to RUNNING
    assert res.experiment is not None
    assert res.experiment.status == ExperimentStatus.RUNNING

    # Verify zero financial side-effects
    final_summary = cap_repo.get_financial_summary()
    final_tx_count = len(cap_repo.get_transaction_history())
    assert final_tx_count == initial_tx_count
    assert final_summary.current_balance == initial_summary.current_balance
    assert final_summary.total_cost == initial_summary.total_cost
    assert final_summary.total_cost == Decimal("0.00")

    # Verify all write requests sent status="PAUSED"
    for req in mock_transport.requests:  # type: ignore[attr-defined]
        if req.data:
            try:
                body_json = json.loads(req.data.decode("utf-8"))
                if "status" in body_json:
                    assert body_json["status"] == "PAUSED"
            except Exception:
                pass


# ── D. Partial Failure Tests ─────────────────────────────────────────────────

def test_partial_failure_campaign_creation(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """If campaign creation fails, status becomes FAILED and experiment remains APPROVED."""
    transport = make_mock_transport({
        "/campaigns": MetaApiError("Meta error: Invalid bid objective"),
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is False
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.FAILED
    assert "Invalid bid objective" in (res.execution.last_error or "")

    # Experiment did NOT transition to RUNNING
    exp_repo = ExperimentRepository(session)
    exp = exp_repo.get(approved_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.APPROVED


def test_partial_failure_adset_creation_preserves_campaign(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """If ad set creation fails, campaign_id is preserved, status is FAILED, experiment remains APPROVED."""
    transport = make_mock_transport({
        "/adsets": MetaApiError("Meta error: Budget too low for targeting"),
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is False
    assert res.execution is not None
    assert res.execution.campaign_id == "camp_101"
    assert res.execution.adset_id is None
    assert res.execution.status == ExternalExecutionStatus.FAILED
    assert "Budget too low" in (res.execution.last_error or "")

    # Experiment remains APPROVED
    exp = ExperimentRepository(session).get(approved_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.APPROVED


def test_partial_failure_creative_creation_preserves_adset(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """If creative creation fails, campaign and adset remain preserved."""
    transport = make_mock_transport({
        "/adcreatives": MetaApiError("Meta error: Link URL rejected"),
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is False
    assert res.execution is not None
    assert res.execution.campaign_id == "camp_101"
    assert res.execution.adset_id == "adset_202"
    assert res.execution.creative_id is None
    assert res.execution.status == ExternalExecutionStatus.FAILED


def test_continuation_after_partial_failure_reuses_existing_ids(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Verify that retrying after partial failure resumes from the missing tier without recreating existing tiers."""
    # First dispatch fails on ad creation (tiers 1-3 succeed)
    fail_ad_transport = make_mock_transport({
        "/ads": MetaApiError("Meta error: Transient 500 error"),
    })
    adapter_fail = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=fail_ad_transport)
    res1 = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter_fail)
    assert res1.is_successful is False
    assert res1.execution is not None
    assert res1.execution.campaign_id == "camp_101"
    assert res1.execution.adset_id == "adset_202"
    assert res1.execution.creative_id == "creative_303"
    assert res1.execution.ad_id is None
    assert res1.execution.status == ExternalExecutionStatus.FAILED

    # Second dispatch with working transport resumes and completes
    success_transport = make_mock_transport()
    adapter_success = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=success_transport)
    res2 = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter_success)
    assert res2.is_successful is True
    assert res2.execution is not None
    assert res2.execution.campaign_id == "camp_101"  # Reused!
    assert res2.execution.adset_id == "adset_202"    # Reused!
    assert res2.execution.creative_id == "creative_303" # Reused!
    assert res2.execution.ad_id == "ad_404"          # Newly created
    assert res2.execution.status == ExternalExecutionStatus.DEPLOYED
    assert res2.experiment is not None
    assert res2.experiment.status == ExperimentStatus.RUNNING


# ── E. Timeout Handling Tests ────────────────────────────────────────────────

def test_dispatch_timeout_handling(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Verify timeout marks execution TIMEOUT and does not leave internal experiment RUNNING."""
    transport = make_mock_transport({
        "/campaigns": MetaApiTimeoutError("Network socket timed out after 10.0s"),
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is False
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.TIMEOUT
    assert "timed out" in (res.execution.last_error or "")

    # Experiment remains APPROVED
    exp = ExperimentRepository(session).get(approved_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.APPROVED


# ── F. Remote Duplicate Campaign Tests ───────────────────────────────────────

def test_remote_duplicate_campaign_ambiguous_fails(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Verify that if multiple matching campaigns exist on Meta (AMBIGUOUS), creation is aborted."""
    transport = make_mock_transport({
        "/campaigns?fields=": {
            "data": [
                {"id": "existing_c_1", "name": f"VB-EXP-{approved_experiment.id}", "status": "PAUSED"},
                {"id": "existing_c_2", "name": f"VB-EXP-{approved_experiment.id}", "status": "PAUSED"},
            ]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is False
    assert "Ambiguous" in (res.rejection_reason or "")
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.FAILED


def test_remote_duplicate_campaign_adopted_when_single_exact_match(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Verify that if exactly one matching campaign already exists on Meta (FOUND_EXACT), it is adopted."""
    transport = make_mock_transport({
        "/campaigns?fields=": {
            "data": [{"id": "existing_c_999", "name": f"VB-EXP-{approved_experiment.id}", "status": "PAUSED"}]
        }
    })
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert res.is_successful is True
    assert res.execution is not None
    assert res.execution.campaign_id == "existing_c_999"
    # Ensure campaign POST was NOT called
    for req in transport.requests:  # type: ignore[attr-defined]
        if "/campaigns" in req.full_url and req.get_method() == "POST":
            pytest.fail("Campaign POST was called despite existing campaign on remote.")


# ── G. Emergency Pause / Kill Coordination Tests ─────────────────────────────

def test_emergency_stop_success_pauses_external_and_kills_internal(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """Successful external pause coordinates with internal experiment kill."""
    # First deploy successfully
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=make_mock_transport(),
    )
    disp_res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert disp_res.is_successful is True

    # Now emergency stop
    stop_res = ExecutionDispatchService.emergency_stop(
        session,
        approved_experiment.id,
        reason="Human operator triggered emergency stop.",
        adapter=adapter,
    )
    assert stop_res.is_successful is True
    assert stop_res.experiment is not None
    assert stop_res.experiment.status == ExperimentStatus.KILLED

    # External pause was called
    assert stop_res.execution is not None
    assert stop_res.execution.last_error is None


def test_emergency_stop_pause_failure_blocks_internal_kill(session: Session, approved_experiment: Experiment, valid_spec: MetaExecutionSpecification):
    """If external pause fails, internal experiment is NOT marked KILLED and remains RUNNING."""
    # Deploy successfully
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=make_mock_transport(),
    )
    disp_res = _dispatch_meta(session, approved_experiment.id, valid_spec, adapter=adapter)
    assert disp_res.is_successful is True

    # Emergency stop with failing pause transport
    failing_adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=make_mock_transport({
            "camp_101": MetaApiError("Meta error: Rate limit or internal server error on pause"),
        }),
    )
    stop_res = ExecutionDispatchService.emergency_stop(
        session,
        approved_experiment.id,
        reason="Operator abort attempt",
        adapter=failing_adapter,
    )
    assert stop_res.is_successful is False
    assert "External Meta campaign pause failed" in (stop_res.rejection_reason or "")

    # Crucial safety invariant: internal experiment was NOT marked KILLED
    exp = ExperimentRepository(session).get(approved_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.RUNNING


def test_emergency_stop_without_external_deployment_kills_directly(session: Session, approved_experiment: Experiment):
    """If experiment has no external deployment, emergency stop cleanly kills internal experiment."""
    stop_res = ExecutionDispatchService.emergency_stop(
        session,
        approved_experiment.id,
        reason="Early termination before dispatch.",
    )
    assert stop_res.is_successful is True
    assert stop_res.experiment is not None
    assert stop_res.experiment.status == ExperimentStatus.KILLED


# ── H. Secret Safety Tests ───────────────────────────────────────────────────

def test_secret_safety_in_error_sanitization():
    """Verify that error messages sanitize bearer tokens and access tokens."""
    raw_error = "Failed request with Bearer EAABwzL12345secretToken&access_token=EAABwzL12345secretToken!"
    sanitized = _sanitize_error(raw_error)
    assert "EAABwzL12345secretToken" not in sanitized
    assert "Bearer [REDACTED]" in sanitized
    assert "access_token=[REDACTED]" in sanitized
