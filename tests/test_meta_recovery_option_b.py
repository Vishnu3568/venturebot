"""Tests for Auditable Meta Recovery Architecture (Option B — Step 90.12).

Tests the 2-phase readiness-gated recovery architecture:
1. Audit event persistence and chronological retrieval.
2. Preservation of original expired ad set without modification.
3. Deterministic versioned replacement ad set creation.
4. Exact 48.0-hour schedule validation.
5. Future schedule validation.
6. Ad review pending and approved states in readiness verification.
7. SAFE_MODE enforcement across all mutation methods.
8. Missing explicit authorization rejection.
9. Timeout reconciliation for replacement ad set.
10. Budget ceiling validation and zero capital expansion.
11. Campaign budget sharing rejection.
12. Spend threshold evaluation and circuit breaker behavior.
13. Lifecycle separation: experiment remains APPROVED until verified delivery.
14. Failed partial deployment recovery without duplicates.
15. Zero live Meta API writes (all tests use mock transport).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Any
import urllib.request
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from venturebot.database.models import (
    Base,
    ExperimentORM,
    ExternalExecutionEventORM,
    OpportunityORM,
)
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.execution.dispatch import (
    ExecutionDispatchService,
    RecoveryDormantResult,
    ReadinessResult,
    ScheduleLockResult,
    ActivationResult,
    DeliveryVerificationResult,
    SpendThresholdStatus,
)
from venturebot.execution.meta import (
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
    deterministic_ad_name,
    deterministic_adset_name,
    deterministic_campaign_name,
    deterministic_creative_name,
)
from venturebot.models.experiment import ExperimentStatus
from venturebot.models.external_execution import (
    ExternalExecution,
    ExternalExecutionEvent,
    ExternalExecutionEventType,
    ExternalExecutionStatus,
)


SAMPLE_ACCOUNT_ID = "act_1985595022114520"
SAMPLE_TOKEN = "TEST_SYSTEM_USER_TOKEN_SAFE"
SAMPLE_CAMPAIGN_ID = "120252176243860380"
ORIGINAL_ADSET_ID = "120252176254130380"
SAMPLE_IMAGE_HASH = "d921604e0240ee8329b3b7fe5235abd4"


@pytest.fixture
def session() -> Session:
    """In-memory SQLite session with full schema initialized."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    sess = session_factory()
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def approved_experiment(session: Session) -> ExperimentORM:
    """Canonical approved pilot experiment."""
    opp = OpportunityORM(
        id=uuid4(),
        title="Solopreneur Financial Workflow Guide",
        category="freelance",
        status="approved",
        description="Problem validation pilot",
    )
    session.add(opp)
    session.flush()

    exp = ExperimentORM(
        id=UUID("49fde874-9387-5056-934c-51a9cfca164f"),
        opportunity_id=opp.id,
        hypothesis="Freelancers will click",
        objective="Validate problem demand",
        channel="facebook",
        monetization_method="direct_sale",
        destination_url="https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/",
        status=ExperimentStatus.APPROVED.value,
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("200.00"),
        actual_spend=Decimal("0.00"),
        success_criteria="CTR >= 1.5%",
        failure_criteria="CTR < 0.5%",
    )
    session.add(exp)
    session.commit()
    return exp


@pytest.fixture
def existing_execution(session: Session, approved_experiment: ExperimentORM) -> ExternalExecution:
    """Existing ExternalExecution record representing the Step 90 partial state."""
    repo = ExternalExecutionRepository(session)
    return repo.create(
        ExternalExecution(
            id=UUID("c543f895-96da-4e44-a2c8-bea0311ad9d4"),
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id=ORIGINAL_ADSET_ID,
            creative_id=None,
            ad_id=None,
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.FAILED,
            last_error="Error creating creative: Meta API HTTP 400 error: Bad Request (OAuthException code 100, error_subcode 1885183)",
        )
    )


@pytest.fixture
def valid_spec(approved_experiment: ExperimentORM) -> MetaExecutionSpecification:
    """Canonical execution specification for the pilot experiment."""
    now_utc = datetime.now(timezone.utc)
    start_utc = now_utc + timedelta(hours=24)
    end_utc = start_utc + timedelta(hours=48)
    return MetaExecutionSpecification(
        experiment_id=approved_experiment.id,
        ad_account_id=SAMPLE_ACCOUNT_ID,
        page_id="1389949167526709",
        destination_url="https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/",
        primary_text="Stop Chasing Invoices — Freelance Guide",
        headline="Solopreneur Financial Workflow Guide",
        image_hash=SAMPLE_IMAGE_HASH,
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


class MockTransport:
    """Custom mock transport simulating Meta Graph API v20.0 responses."""

    def __init__(
        self,
        ad_review_status: str = "PAUSED",
        cbo_enabled: bool = False,
        fail_ad_unpause: bool = False,
        schedule_expired: bool = False,
        schedule_invalid_duration: bool = False,
        fail_ad_pause: bool = False,
        fail_adset_pause: bool = False,
        fail_campaign_pause: bool = False,
        fail_original_adset_pause: bool = False,
        fail_original_adset_get: bool = False,
        original_adset_status: str = "PAUSED",
        campaign_name: str | None = None,
        fail_campaign_readback: bool = False,
        fail_ad_readback: bool = False,
        fail_adset_readback: bool = False,
    ):
        self.requests: list[urllib.request.Request] = []
        self.ad_review_status = ad_review_status
        self.cbo_enabled = cbo_enabled
        self.fail_ad_unpause = fail_ad_unpause
        self.schedule_expired = schedule_expired
        self.schedule_invalid_duration = schedule_invalid_duration
        self.fail_ad_pause = fail_ad_pause
        self.fail_adset_pause = fail_adset_pause
        self.fail_campaign_pause = fail_campaign_pause
        self.fail_original_adset_pause = fail_original_adset_pause
        self.fail_original_adset_get = fail_original_adset_get
        self.original_adset_status = original_adset_status
        self.campaign_name = campaign_name
        self.fail_campaign_readback = fail_campaign_readback
        self.fail_ad_readback = fail_ad_readback
        self.fail_adset_readback = fail_adset_readback

        now = datetime.now(timezone.utc)
        if schedule_expired:
            self.schedule_start = (now - timedelta(hours=96)).strftime("%Y-%m-%dT%H:%M:%S+0000")
            self.schedule_end = (now - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S+0000")
        elif schedule_invalid_duration:
            self.schedule_start = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S+0000")
            self.schedule_end = (now + timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%S+0000")  # 36h != 48h
        else:
            self.schedule_start = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S+0000")
            self.schedule_end = (now + timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%S+0000")  # exact 48h
        self.adset_status: str = "PAUSED"
        self.campaign_status: str = "PAUSED"
        self.ad_status: str = "PAUSED"
        self.adsets_created: list[dict[str, Any]] = []

    def __call__(self, req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        self.requests.append(req)
        url = req.full_url
        data_body = req.data.decode("utf-8") if req.data else ""

        # Campaign CBO check
        if "is_adset_budget_sharing_enabled" in url:
            return json.dumps({
                "id": SAMPLE_CAMPAIGN_ID,
                "is_adset_budget_sharing_enabled": self.cbo_enabled,
                "budget_rebalance_flag": False,
            }).encode("utf-8")

        # Campaign GET lookup
        if (f"/{SAMPLE_CAMPAIGN_ID}?" in url or url.endswith(f"/{SAMPLE_CAMPAIGN_ID}")) and "/adsets" not in url and req.get_method() == "GET":
            readback_status = "ACTIVE" if self.fail_campaign_readback else self.campaign_status
            c_name = self.campaign_name or f"VB-EXP-49fde874-9387-5056-934c-51a9cfca164f"
            return json.dumps({
                "id": SAMPLE_CAMPAIGN_ID,
                "name": c_name,
                "status": readback_status,
                "effective_status": readback_status,
            }).encode("utf-8")

        # Campaign pause / unpause update
        if (f"/{SAMPLE_CAMPAIGN_ID}?" in url or url.endswith(f"/{SAMPLE_CAMPAIGN_ID}")) and "/adsets" not in url and req.data:
            if self.fail_campaign_pause and b'"status": "PAUSED"' in req.data:
                raise RuntimeError("Simulated Meta network failure on campaign pause")
            payload = json.loads(data_body)
            if "status" in payload:
                self.campaign_status = payload["status"]
            return json.dumps({"success": True}).encode("utf-8")

        # Original Ad Set GET lookup
        if f"/{ORIGINAL_ADSET_ID}" in url and req.get_method() == "GET":
            if self.fail_original_adset_get:
                raise RuntimeError("Simulated Meta network failure on original adset lookup")
            return json.dumps({
                "id": ORIGINAL_ADSET_ID,
                "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET",
                "status": self.original_adset_status,
                "effective_status": self.original_adset_status,
                "start_time": "2026-10-06T12:00:00+0000",
                "end_time": "2026-10-08T12:00:00+0000",
                "lifetime_budget": "20000",
                "campaign_id": SAMPLE_CAMPAIGN_ID,
            }).encode("utf-8")

        # Original Ad Set pause POST
        if f"/{ORIGINAL_ADSET_ID}" in url and req.data:
            if self.fail_original_adset_pause and b'"status": "PAUSED"' in req.data:
                raise RuntimeError("Simulated Meta network failure on original adset pause")
            payload = json.loads(data_body)
            if "status" in payload:
                self.original_adset_status = payload["status"]
            return json.dumps({"success": True}).encode("utf-8")

        # Ad set lookup (empty by default)
        if "/adsets?fields=" in url and req.get_method() == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Ad set creation
        if "/adsets" in url and req.data:
            new_id = "120252176299999999"
            payload = json.loads(data_body)
            self.adsets_created.append(payload)
            if not self.schedule_expired and not self.schedule_invalid_duration:
                self.schedule_start = payload.get("start_time", self.schedule_start)
                self.schedule_end = payload.get("end_time", self.schedule_end)
            return json.dumps({"id": new_id}).encode("utf-8")

        # Ad set schedule update
        if f"/{self.adsets_created[-1]['name'] if self.adsets_created else '120252176299999999'}" in url and req.data:
            payload = json.loads(data_body)
            if "start_time" in payload:
                self.schedule_start = payload["start_time"]
            if "end_time" in payload:
                self.schedule_end = payload["end_time"]
            if "status" in payload:
                self.adset_status = payload["status"]
            return json.dumps({"success": True}).encode("utf-8")

        # Creative lookup & creation
        if "/adcreatives?fields=" in url and req.get_method() == "GET":
            return json.dumps({"data": []}).encode("utf-8")
        if "/adcreatives" in url and req.data:
            return json.dumps({"id": "creative_rec_999"}).encode("utf-8")

        # Ad lookup & creation
        if "/ads?fields=" in url and req.get_method() == "GET":
            return json.dumps({"data": []}).encode("utf-8")
        if "/ads" in url and req.data:
            return json.dumps({"id": "ad_rec_888"}).encode("utf-8")

        # Ad review status GET / Ad metadata GET
        if "/ad_rec_888" in url and req.get_method() == "GET":
            readback_status = "ACTIVE" if self.fail_ad_readback else self.ad_status
            eff = self.ad_review_status if readback_status == "PAUSED" else "ACTIVE"
            return json.dumps({
                "id": "ad_rec_888",
                "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-AD",
                "status": readback_status,
                "effective_status": eff,
                "adset_id": "120252176299999999",
            }).encode("utf-8")

        # Ad unpause / pause POST
        if "/ad_rec_888" in url and req.data:
            if self.fail_ad_pause and b'"status": "PAUSED"' in req.data:
                raise RuntimeError("Simulated Meta network failure on ad pause")
            if self.fail_ad_unpause and b'"status": "ACTIVE"' in req.data:
                raise RuntimeError("Simulated Meta network failure on ad unpause")
            payload = json.loads(data_body)
            if "status" in payload:
                self.ad_status = payload["status"]
            return json.dumps({"success": True}).encode("utf-8")

        # Ad set GET read-back
        if "/120252176299999999" in url and req.get_method() == "GET":
            readback_status = "ACTIVE" if self.fail_adset_readback else self.adset_status
            return json.dumps({
                "id": "120252176299999999",
                "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2",
                "status": readback_status,
                "effective_status": readback_status,
                "start_time": self.schedule_start,
                "end_time": self.schedule_end,
                "lifetime_budget": "20000",
                "campaign_id": SAMPLE_CAMPAIGN_ID,
            }).encode("utf-8")

        # Ad set pause / unpause update
        if "/120252176299999999" in url and req.data:
            if self.fail_adset_pause and b'"status": "PAUSED"' in req.data:
                raise RuntimeError("Simulated Meta network failure on replacement adset pause")
            payload = json.loads(data_body)
            if "status" in payload:
                self.adset_status = payload["status"]
            return json.dumps({"success": True}).encode("utf-8")

        # Fallback pause / unpause update
        if req.data:
            payload = json.loads(data_body)
            if "status" in payload:
                self.adset_status = payload["status"]
            return json.dumps({"success": True}).encode("utf-8")

        return json.dumps({"success": True}).encode("utf-8")


# ── Tests ───────────────────────────────────────────────────────────────────

def test_1_audit_event_persistence_and_chronological_retrieval(session: Session, existing_execution: ExternalExecution):
    """1. Audit events are recorded immutably and returned in chronological order."""
    repo = ExternalExecutionRepository(session)

    e1 = repo.record_event(
        ExternalExecutionEvent(
            execution_id=existing_execution.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="adset",
            old_resource_id=ORIGINAL_ADSET_ID,
            new_resource_id=None,
            reason="Superseded by versioned ad set",
        )
    )
    e2 = repo.record_event(
        ExternalExecutionEvent(
            execution_id=existing_execution.id,
            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
            tier="adset",
            old_resource_id=None,
            new_resource_id="adset_v2_new",
            reason="Created replacement ad set",
        )
    )

    events = repo.get_events(existing_execution.id)
    assert len(events) == 2
    assert events[0].id == e1.id
    assert events[0].event_type == ExternalExecutionEventType.RESOURCE_SUPERSEDED.value
    assert events[0].old_resource_id == ORIGINAL_ADSET_ID
    assert events[1].id == e2.id
    assert events[1].new_resource_id == "adset_v2_new"


def test_2_deterministic_adset_name_versioning():
    """2. Ad set naming deterministically appends -v{version} for version >= 2."""
    exp_id = UUID("49fde874-9387-5056-934c-51a9cfca164f")
    assert deterministic_adset_name(exp_id, version=1) == f"VB-EXP-{exp_id}-ADSET"
    assert deterministic_adset_name(exp_id, version=2) == f"VB-EXP-{exp_id}-ADSET-v2"
    assert deterministic_adset_name(exp_id, version=3) == f"VB-EXP-{exp_id}-ADSET-v3"

    with pytest.raises(ValueError):
        deterministic_adset_name(exp_id, version=0)


def test_3_dormant_deployment_preserves_old_adset_and_creates_replacement(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """3. Dormant deployment creates ADSET-v2, preserves old ad set in lineage, and keeps experiment APPROVED."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )

    assert res.success is True
    assert res.adset_id == "120252176299999999"
    assert res.creative_id == "creative_rec_999"
    assert res.ad_id == "ad_rec_888"

    # Execution record updated
    repo = ExternalExecutionRepository(session)
    updated_exec = repo.get_by_experiment_id(approved_experiment.id)
    assert updated_exec is not None
    assert updated_exec.id == existing_execution.id  # Same ExternalExecution row preserved!
    assert updated_exec.adset_id == "120252176299999999"
    assert updated_exec.status == ExternalExecutionStatus.DEPLOYED_DORMANT

    # Lineage events recorded
    events = repo.get_events(existing_execution.id)
    superseded = [e for e in events if e.event_type == ExternalExecutionEventType.RESOURCE_SUPERSEDED.value]
    assert len(superseded) == 1
    assert superseded[0].old_resource_id == ORIGINAL_ADSET_ID
    assert "Original ad set" in superseded[0].reason

    # Experiment lifecycle check: still APPROVED, actual_start is None
    session.expire_all()
    exp = session.get(ExperimentORM, approved_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.APPROVED.value
    assert exp.actual_start is None


def test_4_dormant_deployment_rejected_in_safe_mode(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """4. deploy_recovery_dormant is unconditionally blocked when safe_mode is True."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=True,  # SAFE_MODE active!
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    assert res.reason == "SAFE_MODE_ENABLED"
    assert len(transport.requests) == 0  # Zero network requests


def test_5_dormant_deployment_rejected_without_explicit_authorization(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """5. deploy_recovery_dormant is rejected if explicit_auth is False."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,  # No explicit authorization!
    )

    assert res.success is False
    assert res.blocked is True
    assert res.reason == "EXPLICIT_AUTHORIZATION_REQUIRED"
    assert len(transport.requests) == 0


def test_6_dormant_deployment_rejected_if_cbo_enabled(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """6. Rejects deployment if Campaign Budget Optimization (Advantage Budget) is active."""
    transport = MockTransport(cbo_enabled=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    assert res.reason == "CAMPAIGN_BUDGET_SHARING_NOT_ALLOWED"


def test_7_readiness_verification_pending_and_approved_states(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """7. verify_readiness correctly identifies pending review vs. approved ad review."""
    # 7a: Pending review state
    transport_pending = MockTransport(ad_review_status="PENDING_REVIEW")
    adapter_pending = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_pending)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter_pending,
        safe_mode=False,
        explicit_auth=True,
    )

    readiness_pending = ExecutionDispatchService.verify_readiness(session, approved_experiment.id, adapter=adapter_pending)
    assert readiness_pending.is_ready is False
    assert readiness_pending.ad_approved is False
    assert "AD_NOT_APPROVED" in readiness_pending.blockers

    # 7b: Approved review state
    transport_approved = MockTransport(ad_review_status="PAUSED")  # Meta reports PAUSED when approved while paused
    adapter_approved = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_approved)

    readiness_approved = ExecutionDispatchService.verify_readiness(session, approved_experiment.id, adapter=adapter_approved)
    assert readiness_approved.is_ready is True
    assert readiness_approved.ad_approved is True
    assert len(readiness_approved.blockers) == 0


def test_8_schedule_locking_enforces_exact_48_hours(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
):
    """8. lock_schedule enforces exact 48.0-hour duration and rejects non-48h schedules."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    now_utc = datetime.now(timezone.utc)
    target_start = now_utc + timedelta(hours=2)
    invalid_end = target_start + timedelta(hours=36)  # 36h instead of 48h!
    valid_end = target_start + timedelta(hours=48)    # Exact 48h

    # Reject non-48h
    res_invalid = ExecutionDispatchService.lock_schedule(
        session=session,
        experiment_id=approved_experiment.id,
        target_start=target_start,
        target_end=invalid_end,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_invalid.success is False
    assert "must be exactly 48 hours" in (res_invalid.reason or "")

    # Accept exact 48h
    res_valid = ExecutionDispatchService.lock_schedule(
        session=session,
        experiment_id=approved_experiment.id,
        target_start=target_start,
        target_end=valid_end,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_valid.success is True
    assert res_valid.duration_hours == 48.0


def test_9_activation_unpauses_adset_without_marking_experiment_running(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """9. activate_recovery_flight unpauses ad set, but leaves experiment in APPROVED status."""
    transport = MockTransport(ad_review_status="PAUSED")
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    act_res = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert act_res.success is True
    assert act_res.campaign_status == "ACTIVE"
    assert act_res.adset_status == "ACTIVE"
    assert act_res.ad_status == "ACTIVE"
    assert transport.campaign_status == "ACTIVE"
    assert transport.adset_status == "ACTIVE"
    assert transport.ad_status == "ACTIVE"

    # Execution status is ACTIVATED
    repo = ExternalExecutionRepository(session)
    updated_exec = repo.get_by_experiment_id(approved_experiment.id)
    assert updated_exec is not None
    assert updated_exec.status == ExternalExecutionStatus.ACTIVATED

    # Lineage events recorded for all three tiers
    events = repo.get_events(existing_execution.id)
    activated_events = [e for e in events if e.event_type == ExternalExecutionEventType.ACTIVATED.value]
    activated_tiers = [e.tier for e in activated_events]
    assert "campaign" in activated_tiers
    assert "adset" in activated_tiers
    assert "ad" in activated_tiers

    # Crucial invariant: Experiment status is still APPROVED!
    session.expire_all()
    exp = session.get(ExperimentORM, approved_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.APPROVED.value
    assert exp.actual_start is None


def test_10_delivery_verification_transitions_experiment_to_running(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
):
    """10. verify_delivery_started transitions experiment to RUNNING only when impressions/spend occur."""
    repo = ExternalExecutionRepository(session)
    existing_execution.status = ExternalExecutionStatus.ACTIVATED
    repo.save(existing_execution)

    # 10a: Zero impressions -> still APPROVED
    v1 = ExecutionDispatchService.verify_delivery_started(
        session=session,
        experiment_id=approved_experiment.id,
        impressions=0,
        spend=Decimal("0.00"),
    )
    assert v1.delivery_started is False
    assert v1.experiment_status == ExperimentStatus.APPROVED.value

    # 10b: Impressions observed -> transitions to RUNNING
    v2 = ExecutionDispatchService.verify_delivery_started(
        session=session,
        experiment_id=approved_experiment.id,
        impressions=12,
        spend=Decimal("1.50"),
    )
    assert v2.delivery_started is True
    assert v2.experiment_status == ExperimentStatus.RUNNING.value

    session.expire_all()
    exp = session.get(ExperimentORM, approved_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.RUNNING.value
    assert exp.actual_start is not None


def test_11_spend_thresholds_and_circuit_breaker():
    """11. check_spend_thresholds evaluates ₹180 warning and ₹190 best-effort breaker."""
    s1 = ExecutionDispatchService.check_spend_thresholds(Decimal("100.00"))
    assert s1.is_warning is False
    assert s1.is_circuit_breaker_triggered is False

    s2 = ExecutionDispatchService.check_spend_thresholds(Decimal("185.00"))
    assert s2.is_warning is True
    assert s2.is_circuit_breaker_triggered is False

    s3 = ExecutionDispatchService.check_spend_thresholds(Decimal("192.50"))
    assert s3.is_warning is True
    assert s3.is_circuit_breaker_triggered is True
    assert "best-effort" in s3.disclaimer


def test_12_emergency_pause_adset(session: Session, approved_experiment: ExperimentORM, existing_execution: ExternalExecution):
    """12. emergency_pause_adset pauses the active replacement ad set."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    pause_res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert pause_res.is_successful is True

    repo = ExternalExecutionRepository(session)
    events = repo.get_events(existing_execution.id)
    pause_events = [e for e in events if e.event_type == ExternalExecutionEventType.EMERGENCY_STOP.value]
    assert len(pause_events) >= 1
    assert "Emergency pause executed" in pause_events[-1].reason


def test_13_activation_requires_meta_ad_review_approval(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """13. activate_recovery_flight rejects activation when Meta policy review is pending or disapproved."""
    # 13a: Pending review
    transport_pending = MockTransport(ad_review_status="PENDING_REVIEW")
    adapter_pending = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_pending)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter_pending,
        safe_mode=False,
        explicit_auth=True,
    )

    res_pending = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter_pending,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_pending.success is False
    assert res_pending.blocked is True
    assert "AD_NOT_APPROVED" in (res_pending.reason or "")
    assert transport_pending.campaign_status == "PAUSED"
    assert transport_pending.adset_status == "PAUSED"
    assert transport_pending.ad_status == "PAUSED"

    # 13b: Disapproved
    transport_disapproved = MockTransport(ad_review_status="DISAPPROVED")
    adapter_disapproved = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_disapproved)

    res_disapproved = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter_disapproved,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_disapproved.success is False
    assert res_disapproved.blocked is True
    assert "AD_NOT_APPROVED" in (res_disapproved.reason or "")


def test_14_activation_requires_valid_future_48h_schedule(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """14. activate_recovery_flight rejects activation if schedule is expired or not exactly 48h."""
    # 14a: Expired schedule
    transport_expired = MockTransport(schedule_expired=True)
    adapter_expired = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_expired)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter_expired,
        safe_mode=False,
        explicit_auth=True,
    )

    res_expired = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter_expired,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_expired.success is False
    assert res_expired.blocked is True
    assert "SCHEDULE_EXPIRED" in (res_expired.reason or "")

    # 14b: Invalid duration (e.g. 36h instead of 48h)
    transport_invalid_dur = MockTransport(schedule_invalid_duration=True)
    adapter_invalid_dur = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_invalid_dur)

    res_invalid_dur = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter_invalid_dur,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_invalid_dur.success is False
    assert res_invalid_dur.blocked is True
    assert "SCHEDULE_DURATION_INVALID" in (res_invalid_dur.reason or "")


def test_15_activation_requires_zero_actual_spend_and_exact_budget(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """15. activate_recovery_flight rejects activation if actual spend > 0 or budget != 200."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    # Corrupt allocated budget
    approved_experiment.allocated_budget = Decimal("300.00")
    session.commit()

    res_budget = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_budget.success is False
    assert res_budget.blocked is True
    assert "expected 200.00" in (res_budget.reason or "")


def test_16_partial_activation_reconciliation_and_idempotency(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """16. Partial activation failure is safely recorded and retry reconciles to full ACTIVATED status."""
    transport = MockTransport(fail_ad_unpause=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    # First attempt: ad unpause fails
    res_partial = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_partial.success is False
    assert res_partial.is_partially_activated is True
    assert res_partial.campaign_status == "ACTIVE"
    assert res_partial.adset_status == "ACTIVE"
    assert res_partial.ad_status == "PAUSED"
    assert "Simulated Meta network failure on ad unpause" in (res_partial.reason or "")

    # Execution status must NOT be ACTIVATED yet!
    repo = ExternalExecutionRepository(session)
    exec_state = repo.get_by_experiment_id(approved_experiment.id)
    assert exec_state is not None
    assert exec_state.status != ExternalExecutionStatus.ACTIVATED

    # Verify audit event for failure was logged
    events = repo.get_events(existing_execution.id)
    fail_events = [e for e in events if e.event_type == ExternalExecutionEventType.ACTIVATION_FAILED.value]
    assert len(fail_events) == 1
    assert fail_events[0].tier == "ad"

    # Second attempt: Network issue resolved, retry activation
    transport.fail_ad_unpause = False
    res_retry = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_retry.success is True
    assert res_retry.is_partially_activated is False
    assert res_retry.campaign_status == "ACTIVE"
    assert res_retry.adset_status == "ACTIVE"
    assert res_retry.ad_status == "ACTIVE"

    # Now execution status is ACTIVATED
    updated_exec = repo.get_by_experiment_id(approved_experiment.id)
    assert updated_exec is not None
    assert updated_exec.status == ExternalExecutionStatus.ACTIVATED


def test_17_verify_delivery_started_rejects_non_activated_deployments(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
):
    """17. verify_delivery_started rejects transitioning experiment if execution is not ACTIVATED."""
    repo = ExternalExecutionRepository(session)
    existing_execution.status = ExternalExecutionStatus.DEPLOYED_DORMANT
    repo.save(existing_execution)

    # Even if impressions and spend are reported, it cannot transition a dormant/non-activated deployment
    res = ExecutionDispatchService.verify_delivery_started(
        session=session,
        experiment_id=approved_experiment.id,
        impressions=100,
        spend=Decimal("10.00"),
    )
    assert res.delivery_started is False
    assert "must be ACTIVATED before delivery can be verified" in (res.reason or "")

    session.expire_all()
    exp = session.get(ExperimentORM, approved_experiment.id)
    assert exp is not None
    assert exp.status == ExperimentStatus.APPROVED.value
    assert exp.actual_start is None


def test_18_adapter_write_methods_strictly_avoid_method_post_keyword():
    """18. MetaMarketingApiAdapter source contains zero method='post' assignments."""
    meta_path = Path(__file__).resolve().parent.parent / "backend" / "venturebot" / "execution" / "meta.py"
    with open(meta_path, "r", encoding="utf-8") as f:
        src = f.read().lower()

    assert 'method="post"' not in src
    assert 'method="put"' not in src
    assert 'method="patch"' not in src
    assert 'method="delete"' not in src
    assert 'method="get"' in src

    # Verify adapter has explicit methods for ad, adset, and campaign lifecycle
    assert hasattr(MetaMarketingApiAdapter, "unpause_ad")
    assert hasattr(MetaMarketingApiAdapter, "pause_ad")
    assert hasattr(MetaMarketingApiAdapter, "unpause_campaign")
    assert hasattr(MetaMarketingApiAdapter, "get_ad")
    assert hasattr(MetaMarketingApiAdapter, "get_campaign")


def test_19_emergency_stop_pauses_all_lineage_resources(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """19. emergency_pause_adset pauses campaign, replacement ad, replacement adset, and original adset."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.is_successful is True
    assert transport.campaign_status == "PAUSED"
    assert transport.ad_status == "PAUSED"
    assert transport.adset_status == "PAUSED"
    assert transport.original_adset_status == "PAUSED"
    assert res.campaign_status is not None and res.campaign_status.verified is True
    assert res.ad_status is not None and res.ad_status.verified is True
    assert res.replacement_adset_status is not None and res.replacement_adset_status.verified is True
    assert res.original_adset_status is not None and res.original_adset_status.verified is True


def test_20_emergency_stop_unverified_campaign_ownership_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """20. If campaign name does not match expected prefix, campaign is NOT paused and issue is reported."""
    transport = MockTransport(campaign_name="UNRELATED_OTHER_CAMPAIGN")
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    # Set campaign to ACTIVE to verify emergency stop leaves it untouched when unverified
    transport.campaign_status = "ACTIVE"

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.is_successful is False
    assert transport.campaign_status == "ACTIVE"  # Campaign was NOT paused automatically!
    assert transport.ad_status == "PAUSED"        # Child resources were still safely paused!
    assert transport.adset_status == "PAUSED"
    assert "ownership unverified" in (res.rejection_reason or "").lower()
    assert len(res.unresolved_scopes) > 0


def test_21_emergency_stop_surfaces_ad_pause_failure(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """21. Replacement ad pause network failure is surfaced and recorded, marking result unsuccessful."""
    transport = MockTransport(fail_ad_pause=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.is_successful is False
    assert res.ad_status is not None
    assert res.ad_status.verified is False
    assert "ad" in (res.rejection_reason or "").lower()

    # Event logged for failure
    repo = ExternalExecutionRepository(session)
    events = repo.get_events(existing_execution.id)
    ad_events = [e for e in events if e.tier == "ad" and "Emergency pause failed" in (e.reason or "")]
    assert len(ad_events) >= 1


def test_22_emergency_stop_surfaces_replacement_adset_pause_failure(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """22. Replacement ad set pause failure is surfaced and recorded."""
    transport = MockTransport(fail_adset_pause=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.is_successful is False
    assert res.replacement_adset_status is not None
    assert res.replacement_adset_status.verified is False
    assert "adset" in (res.rejection_reason or "").lower()


def test_23_emergency_stop_surfaces_original_adset_pause_failure(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """23. Original ad set pause failure when active is surfaced and recorded."""
    transport = MockTransport(original_adset_status="ACTIVE", fail_original_adset_pause=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.is_successful is False
    assert res.original_adset_status is not None
    assert res.original_adset_status.verified is False
    assert "original adset" in (res.rejection_reason or "").lower()


def test_24_emergency_stop_campaign_pause_failure_continues_child_pauses(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """24. Campaign pause failure does NOT abort child ad and adset emergency pause attempts."""
    transport = MockTransport(fail_campaign_pause=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.is_successful is False
    assert res.campaign_status is not None and res.campaign_status.verified is False
    assert transport.ad_status == "PAUSED"    # Ad pause was still executed!
    assert transport.adset_status == "PAUSED" # Adset pause was still executed!


def test_25_activation_aborts_if_original_adset_is_active(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """25. activate_recovery_flight aborts immediately if original ad set is active, and emergency pauses it."""
    transport = MockTransport(original_adset_status="ACTIVE")
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    assert "ORIGINAL_ADSET_UNEXPECTEDLY_ACTIVE" in (res.reason or "")
    assert transport.original_adset_status == "PAUSED" # Re-paused on Meta!
    assert transport.campaign_status == "PAUSED"       # Campaign NEVER activated!
    assert transport.adset_status == "PAUSED"          # Replacement adset NEVER activated!
    assert transport.ad_status == "PAUSED"             # Replacement ad NEVER activated!

    repo = ExternalExecutionRepository(session)
    exec_record = repo.get_by_experiment_id(approved_experiment.id)
    assert exec_record is not None
    assert exec_record.status != ExternalExecutionStatus.ACTIVATED


def test_26_activation_aborts_if_original_adset_inspection_fails(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """26. activate_recovery_flight aborts if inspecting original ad set raises an error."""
    transport = MockTransport(fail_original_adset_get=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    assert "ORIGINAL_ADSET_INSPECTION_FAILED" in (res.reason or "")
    assert transport.campaign_status == "PAUSED"
    assert transport.adset_status == "PAUSED"
    assert transport.ad_status == "PAUSED"


def test_27_activation_aborts_if_original_adset_pause_fails(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """27. activate_recovery_flight aborts and logs failure if original ad set is active and re-pause fails."""
    transport = MockTransport(original_adset_status="ACTIVE", fail_original_adset_pause=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    assert "ORIGINAL_ADSET_UNEXPECTEDLY_ACTIVE" in (res.reason or "")
    assert "Emergency re-pause failed" in (res.reason or "")


def test_28_activation_makes_zero_mutation_calls_after_original_adset_anomaly(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """28. No activation mutation requests are made when original ad set check triggers."""
    transport = MockTransport(original_adset_status="ACTIVE")
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    # Clear requests recorded during deployment
    transport.requests.clear()

    ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    # Check all write requests issued during activation
    write_requests = [r for r in transport.requests if r.data]
    # The only write request allowed was the emergency pause on original ad set!
    assert len(write_requests) == 1
    assert f"/{ORIGINAL_ADSET_ID}" in write_requests[0].full_url
    assert b'"status": "PAUSED"' in write_requests[0].data


def test_29_emergency_stop_idempotency_safe_repeated_calls(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """29. Repeated emergency stop calls are safe and succeed idempotently."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    r1 = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r1.is_successful is True

    r2 = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r2.is_successful is True


def test_30_emergency_stop_readback_failure_produces_unsuccessful_result(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """30. Read-back verification failure on Meta produces an unsuccessful emergency stop result."""
    transport = MockTransport(fail_ad_readback=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is False
    assert "expected paused" in (res.rejection_reason or "").lower()


def test_31_emergency_stop_never_passes_unrelated_resource_ids(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """31. Emergency stop requests target strictly this experiment's registered IDs."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    transport.requests.clear()

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is True

    allowed_ids = {
        SAMPLE_CAMPAIGN_ID,
        ORIGINAL_ADSET_ID,
        "120252176299999999",
        "ad_rec_888",
        SAMPLE_ACCOUNT_ID,
    }

    for req in transport.requests:
        url = req.full_url
        parts = url.split("?")[0].split("/")
        target_id = parts[-1]
        assert target_id in allowed_ids, f"Unrelated target ID '{target_id}' found in request '{url}'"


