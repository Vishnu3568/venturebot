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
import os
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
    ExternalExecutionORM,
    OpportunityORM,
)
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.execution.dispatch import (
    ActivationResult,
    DeliveryVerificationResult,
    ExecutionDispatchService,
    MetaExperimentDispatchService,
    ReadinessResult,
    RecoveryDormantResult,
    RecoveryLineageClassification,
    ScheduleLockResult,
    SpendThresholdStatus,
    classify_execution_lineage,
    validate_meta_account_id,
)
from venturebot.execution.meta import (
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
    TokenInspectionResult,
    TokenStatus,
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

os.environ["META_APP_ACCESS_TOKEN"] = "TEST_APP_ACCESS_TOKEN_SAFE"
os.environ["META_APP_ID"] = "1063651013045060"


SAMPLE_ACCOUNT_ID = "act_1985595022114520"
SAMPLE_TOKEN = "TEST_SYSTEM_USER_TOKEN_SAFE"
SAMPLE_CAMPAIGN_ID = "120252176243860380"
ORIGINAL_ADSET_ID = "120252176254130380"
SAMPLE_IMAGE_HASH = "d921604e0240ee8329b3b7fe5235abd4"


@pytest.fixture(autouse=True)
def _enable_recovery_mock_testing(monkeypatch: pytest.MonkeyPatch):
    """Explicitly configure safe mode disabled for Option B mocked recovery tests."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "false")


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
        campaign_id_override: str | None = None,
        campaign_account_id: str | None = None,
        omit_campaign_account_id: bool = False,
        omit_campaign_name: bool = False,
        fail_token_inspection: bool = False,
        malformed_token_response: bool = False,
        token_is_invalid: bool = False,
        token_expired: bool = False,
        token_near_expiry: bool = False,
        token_never_expires: bool = False,
        fail_inspector_auth: bool = False,
        inspector_wrong_app: bool = False,
        token_mismatched_app_id: bool = False,
        incompatible_token_type: bool = False,
        missing_required_scopes: bool = False,
        granular_scopes_unauthorized: bool = False,
        granular_scopes_authorized: bool = False,
        data_access_expired: bool = False,
        data_access_near_expiry: bool = False,
        malformed_data_access: bool = False,
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
        self.campaign_id_override = campaign_id_override
        self.campaign_account_id = campaign_account_id
        self.omit_campaign_account_id = omit_campaign_account_id
        self.omit_campaign_name = omit_campaign_name
        self.campaign_pause_calls: int = 0
        self.fail_token_inspection = fail_token_inspection
        self.malformed_token_response = malformed_token_response
        self.token_is_invalid = token_is_invalid
        self.token_expired = token_expired
        self.token_near_expiry = token_near_expiry
        self.token_never_expires = token_never_expires
        self.fail_inspector_auth = fail_inspector_auth
        self.inspector_wrong_app = inspector_wrong_app
        self.token_mismatched_app_id = token_mismatched_app_id
        self.incompatible_token_type = incompatible_token_type
        self.missing_required_scopes = missing_required_scopes
        self.granular_scopes_unauthorized = granular_scopes_unauthorized
        self.granular_scopes_authorized = granular_scopes_authorized
        self.data_access_expired = data_access_expired
        self.data_access_near_expiry = data_access_near_expiry
        self.malformed_data_access = malformed_data_access

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

        # /debug_token handling
        if "/debug_token" in url and req.get_method() == "GET":
            if self.fail_token_inspection:
                raise RuntimeError("Simulated Meta network failure on debug_token")
            if self.fail_inspector_auth:
                return json.dumps({
                    "error": {
                        "message": "Invalid OAuth access token - cannot inspect token with invalid inspector credential.",
                        "type": "OAuthException",
                        "code": 190,
                    }
                }).encode("utf-8")
            if self.inspector_wrong_app:
                return json.dumps({
                    "error": {
                        "message": "Application does not have permission to inspect this token.",
                        "type": "OAuthException",
                        "code": 10,
                    }
                }).encode("utf-8")
            if self.malformed_token_response:
                return json.dumps({"unexpected": "structure"}).encode("utf-8")
            if self.token_is_invalid:
                return json.dumps({
                    "data": {
                        "is_valid": False,
                        "error": {"message": "Invalid OAuth access token."},
                    }
                }).encode("utf-8")
            now_ts = int(datetime.now(timezone.utc).timestamp())

            app_id = "999999999999999" if self.token_mismatched_app_id else "1063651013045060"
            token_type = "PAGE" if self.incompatible_token_type else "SYSTEM_USER"
            if self.missing_required_scopes:
                scopes = ["email", "public_profile"]
            else:
                scopes = ["ads_management", "ads_read"]

            granular_scopes = None
            if self.granular_scopes_unauthorized:
                granular_scopes = [
                    {"scope": "ads_management", "target_ids": ["999999999999"]},
                    {"scope": "ads_read", "target_ids": ["999999999999"]},
                ]
            elif self.granular_scopes_authorized:
                granular_scopes = [
                    {"scope": "ads_management", "target_ids": ["1985595022114520"]},
                    {"scope": "ads_read", "target_ids": ["1985595022114520"]},
                ]

            if self.data_access_expired:
                da_ts = now_ts - 3600
            elif self.data_access_near_expiry:
                da_ts = now_ts + (20 * 3600)
            elif self.malformed_data_access:
                da_ts = "not-a-timestamp"
            else:
                da_ts = now_ts + (90 * 86400)

            if self.token_expired:
                exp_ts = now_ts - 3600  # expired 1 hour ago
            elif self.token_near_expiry:
                exp_ts = now_ts + (20 * 3600)  # 20 hours remaining (< 72h required)
            elif self.token_never_expires:
                exp_ts = 0  # permanent System User token
            else:
                exp_ts = now_ts + (90 * 86400)  # 90 days remaining (> 72h required)

            data_obj: dict[str, Any] = {
                "app_id": app_id,
                "type": token_type,
                "application": "VentureBot",
                "data_access_expires_at": da_ts,
                "expires_at": exp_ts,
                "is_valid": True,
                "scopes": scopes,
                "user_id": "987654321",
            }
            if granular_scopes is not None:
                data_obj["granular_scopes"] = granular_scopes

            return json.dumps({"data": data_obj}).encode("utf-8")

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
            resp_dict: dict[str, Any] = {
                "id": self.campaign_id_override or SAMPLE_CAMPAIGN_ID,
                "name": c_name,
                "status": readback_status,
                "effective_status": readback_status,
            }
            if not self.omit_campaign_account_id:
                resp_dict["account_id"] = (
                    self.campaign_account_id
                    if self.campaign_account_id is not None
                    else SAMPLE_ACCOUNT_ID.removeprefix("act_")
                )
            if self.omit_campaign_name:
                resp_dict.pop("name", None)
            return json.dumps(resp_dict).encode("utf-8")

        # Campaign pause / unpause update
        if (f"/{SAMPLE_CAMPAIGN_ID}?" in url or url.endswith(f"/{SAMPLE_CAMPAIGN_ID}")) and "/adsets" not in url and req.data:
            self.campaign_pause_calls += 1
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


# ── Step 90.12.5: Multi-Factor Campaign Ownership Tests ───────────────────────

def test_32_emergency_stop_exclusive_ownership_permits_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """32. Valid exclusive ownership across DB, lineage, and remote metadata permits campaign pausing."""
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
    assert res.campaign_status is not None
    assert res.campaign_status.paused is True
    assert res.campaign_status.verified is True
    assert transport.campaign_pause_calls == 1


def test_33_emergency_stop_multiple_current_executions_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """33. Multiple execution records referencing the same campaign blocks campaign pause."""
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

    # Create a second experiment and execution referencing the same campaign ID
    other_exp = ExperimentORM(
        id=uuid4(),
        opportunity_id=approved_experiment.opportunity_id,
        hypothesis="Duplicate hypothesis",
        objective="Duplicate objective",
        channel="facebook",
        monetization_method="direct_sale",
        destination_url="https://example.com/duplicate",
        status=ExperimentStatus.APPROVED.value,
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("200.00"),
        actual_spend=Decimal("0.00"),
        success_criteria="CTR >= 1.5%",
        failure_criteria="CTR < 0.5%",
    )
    session.add(other_exp)
    session.flush()

    other_exec = ExternalExecutionORM(
        id=uuid4(),
        experiment_id=other_exp.id,
        channel="meta",
        external_account_id=SAMPLE_ACCOUNT_ID,
        campaign_id=SAMPLE_CAMPAIGN_ID,
        status="deployed_dormant",
    )
    session.add(other_exec)
    session.commit()

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is False
    assert transport.campaign_pause_calls == 0
    assert res.campaign_status is not None and res.campaign_status.paused is False
    assert "distinct current executions" in (res.rejection_reason or "").lower()
    # Child resources are still safely paused
    assert res.ad_status is not None and res.ad_status.paused is True
    assert res.replacement_adset_status is not None and res.replacement_adset_status.paused is True


def test_34_emergency_stop_no_current_execution_found_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
    monkeypatch: pytest.MonkeyPatch,
):
    """34. When repository reports zero matching executions, campaign pause is safely blocked."""
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

    # Monkeypatch get_by_campaign_id to simulate zero matches
    monkeypatch.setattr(ExternalExecutionRepository, "get_by_campaign_id", lambda self, cid: [])

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is False
    assert transport.campaign_pause_calls == 0
    assert "no current execution records reference campaign" in (res.rejection_reason or "").lower()


def test_35_emergency_stop_same_execution_historical_events_permitted(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """35. Historical audit events belonging to the same execution do not block campaign pausing."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    deploy_res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert deploy_res.execution is not None

    repo = ExternalExecutionRepository(session)
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=deploy_res.execution.id,
            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
            tier="campaign",
            new_resource_id=SAMPLE_CAMPAIGN_ID,
            reason="Historical campaign created event",
        )
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is True
    assert transport.campaign_pause_calls == 1


def test_36_emergency_stop_conflicting_historical_event_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """36. Historical audit event on another execution referencing this campaign blocks campaign pause."""
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

    # Insert conflicting event on a foreign execution ID
    foreign_execution_id = uuid4()
    other_exp = ExperimentORM(
        id=uuid4(),
        opportunity_id=approved_experiment.opportunity_id,
        hypothesis="Foreign hypothesis",
        objective="Foreign objective",
        channel="facebook",
        monetization_method="direct_sale",
        destination_url="https://example.com/foreign",
        status=ExperimentStatus.APPROVED.value,
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("200.00"),
        actual_spend=Decimal("0.00"),
        success_criteria="CTR >= 1.5%",
        failure_criteria="CTR < 0.5%",
    )
    session.add(other_exp)
    session.flush()

    foreign_exec = ExternalExecutionORM(
        id=foreign_execution_id,
        experiment_id=other_exp.id,
        channel="meta",
        external_account_id=SAMPLE_ACCOUNT_ID,
        campaign_id="camp_other_999",
        status="deployed",
    )
    session.add(foreign_exec)
    session.flush()

    repo = ExternalExecutionRepository(session)
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=foreign_execution_id,
            event_type=ExternalExecutionEventType.RESOURCE_ADOPTED.value,
            tier="campaign",
            new_resource_id=SAMPLE_CAMPAIGN_ID,
            reason="Foreign execution claimed campaign",
        )
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is False
    assert transport.campaign_pause_calls == 0
    assert "conflicting historical event" in (res.rejection_reason or "").lower()
    # Child resources still safely paused
    assert res.ad_status is not None and res.ad_status.paused is True
    assert res.replacement_adset_status is not None and res.replacement_adset_status.paused is True


def test_37_emergency_stop_campaign_superseded_on_same_execution_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """37. If campaign was superseded on this execution, campaign pause is safely blocked."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    deploy_res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert deploy_res.execution is not None

    repo = ExternalExecutionRepository(session)
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=deploy_res.execution.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="campaign",
            old_resource_id=SAMPLE_CAMPAIGN_ID,
            new_resource_id="new_camp_replaced_777",
            reason="Campaign was superseded by replacement campaign",
        )
    )

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is False
    assert transport.campaign_pause_calls == 0
    assert "was superseded" in (res.rejection_reason or "").lower()


def test_38_emergency_stop_remote_campaign_id_mismatch_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """38. Remote campaign ID mismatch blocks campaign pause."""
    transport = MockTransport(campaign_id_override="DIFFERENT_REMOTE_CAMP_ID")
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
    assert transport.campaign_pause_calls == 0
    assert "remote campaign id 'different_remote_camp_id' does not match" in (res.rejection_reason or "").lower()


def test_39_emergency_stop_remote_account_id_mismatch_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """39. Remote account ID mismatch blocks campaign pause."""
    transport = MockTransport(campaign_account_id="999888777")
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
    assert transport.campaign_pause_calls == 0
    assert "remote campaign account '999888777' does not match" in (res.rejection_reason or "").lower()


def test_40_emergency_stop_missing_remote_account_id_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """40. Missing remote account ID blocks campaign pause."""
    transport = MockTransport(omit_campaign_account_id=True)
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
    assert transport.campaign_pause_calls == 0
    assert (
        "remote account id 'none' is missing or malformed" in (res.rejection_reason or "").lower()
        or "remote account id '' is missing or malformed" in (res.rejection_reason or "").lower()
    )


def test_41_emergency_stop_malformed_local_account_id_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """41. Malformed local account ID blocks campaign pause."""
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

    # Set local account to malformed value
    orm = session.get(ExternalExecutionORM, existing_execution.id)
    assert orm is not None
    orm.external_account_id = "!bad@account#"
    session.commit()

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is False
    assert transport.campaign_pause_calls == 0
    assert "local account id '!bad@account#' is missing or malformed" in (res.rejection_reason or "").lower()


def test_42_emergency_stop_missing_campaign_name_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """42. Missing remote campaign name blocks campaign pause."""
    transport = MockTransport(omit_campaign_name=True)
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
    assert transport.campaign_pause_calls == 0
    assert "remote campaign name is missing or empty" in (res.rejection_reason or "").lower()


def test_43_emergency_stop_name_prefix_match_with_account_mismatch_blocks_campaign_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """43. Name prefix matching but remote account mismatching blocks campaign pause."""
    transport = MockTransport(
        campaign_name=f"VB-EXP-{approved_experiment.id}-VALID-SUFFIX",
        campaign_account_id="777666555",
    )
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
    assert transport.campaign_pause_calls == 0
    assert "remote campaign account '777666555' does not match" in (res.rejection_reason or "").lower()


def test_44_emergency_stop_invalid_ownership_never_calls_pause_campaign(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """44. pause_campaign is never called when ownership verification fails."""
    for bad_transport in [
        MockTransport(campaign_name="UNRELATED"),
        MockTransport(campaign_account_id="111222333"),
        MockTransport(campaign_id_override="DIFFERENT_ID"),
    ]:
        bad_adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=bad_transport)
        res = ExecutionDispatchService.emergency_pause_adset(
            session=session,
            experiment_id=approved_experiment.id,
            adapter=bad_adapter,
            safe_mode=False,
            explicit_auth=True,
        )
        assert res.is_successful is False
        assert bad_transport.campaign_pause_calls == 0


def test_45_emergency_stop_child_resources_continue_when_campaign_unverified(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """45. Child emergency stopping continues safely even when campaign ownership cannot be established."""
    transport = MockTransport(campaign_account_id="999999999")
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    # Set remote objects to ACTIVE to prove emergency stop pauses them
    transport.ad_status = "ACTIVE"
    transport.adset_status = "ACTIVE"
    transport.original_adset_status = "ACTIVE"

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is False
    assert res.campaign_status is not None and res.campaign_status.paused is False
    assert transport.campaign_pause_calls == 0

    # Child resources were all safely paused and verified
    assert res.ad_status is not None and res.ad_status.paused is True and res.ad_status.verified is True
    assert res.replacement_adset_status is not None and res.replacement_adset_status.paused is True and res.replacement_adset_status.verified is True
    assert res.original_adset_status is not None and res.original_adset_status.paused is True and res.original_adset_status.verified is True


def test_46_meta_adapter_get_campaign_requests_account_id(
    session: Session,
):
    """46. MetaMarketingApiAdapter.get_campaign requests account_id in query fields."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    adapter.get_campaign(SAMPLE_CAMPAIGN_ID)
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert "fields=" in req.full_url
    assert "account_id" in req.full_url
    assert "id" in req.full_url
    assert "name" in req.full_url
    assert "status" in req.full_url
    assert "effective_status" in req.full_url


# ==============================================================================
# Step 90.12.8 Hardened Tests: Account ID, Token Preflight, Emergency Stop, Versioning
# ==============================================================================


def test_47_validate_meta_account_id_valid_formats():
    """47. validate_meta_account_id accepts plain numeric and act_-prefixed string formats."""
    # Plain numeric
    assert validate_meta_account_id("1985595022114520") == "1985595022114520"
    # With single act_ prefix
    assert validate_meta_account_id("act_1985595022114520") == "1985595022114520"
    # Leading and trailing whitespace trimmed
    assert validate_meta_account_id("  1985595022114520 \t\n") == "1985595022114520"
    assert validate_meta_account_id("  act_1985595022114520  ") == "1985595022114520"


def test_48_validate_meta_account_id_rejects_non_string_types():
    """48. validate_meta_account_id strictly rejects non-string types without str() coercion."""
    assert validate_meta_account_id(1985595022114520) is None
    assert validate_meta_account_id(1985595022114520.0) is None
    assert validate_meta_account_id(True) is None
    assert validate_meta_account_id(False) is None
    assert validate_meta_account_id(["1985595022114520"]) is None
    assert validate_meta_account_id({"account_id": "1985595022114520"}) is None
    assert validate_meta_account_id(None) is None


def test_49_validate_meta_account_id_rejects_malformed_strings():
    """49. validate_meta_account_id rejects empty, alphabetic, unicode, punctuation, and repeated prefixes."""
    # Empty and whitespace
    assert validate_meta_account_id("") is None
    assert validate_meta_account_id("   ") is None
    assert validate_meta_account_id("\t\n") is None
    # Prefix only
    assert validate_meta_account_id("act_") is None
    # Repeated prefixes
    assert validate_meta_account_id("act_act_1985595022114520") is None
    assert validate_meta_account_id("act_act_123") is None
    # Letters / alphanumeric
    assert validate_meta_account_id("act_19855abc") is None
    assert validate_meta_account_id("abcdef") is None
    # Embedded whitespace
    assert validate_meta_account_id("act_ 1985595022114520") is None
    assert validate_meta_account_id("19855 95022114520") is None
    # Punctuation
    assert validate_meta_account_id("19855-95022114520") is None
    assert validate_meta_account_id("19855.95022114520") is None
    # Non-ASCII digits (Arabic-Indic, Full-width)
    assert validate_meta_account_id("\u0661\u0662\u0663\u0664\u0665") is None
    assert validate_meta_account_id("\uff11\uff12\uff13") is None


def test_50_campaign_ownership_equivalent_ids_with_different_prefixes_succeed(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """50. Ownership check succeeds when remote has plain numeric and local has act_ prefix."""
    # Remote has plain numeric ID (no act_ prefix)
    transport = MockTransport(campaign_account_id="1985595022114520")
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
    assert res.campaign_status is not None and res.campaign_status.paused is True
    assert transport.campaign_pause_calls == 1


def test_51_campaign_ownership_rejects_integer_local_account_id(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
    monkeypatch: pytest.MonkeyPatch,
):
    """51. If local external_account_id is not a string, campaign pause is blocked."""
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

    # Use monkeypatch on ExternalExecutionRepository.get_by_experiment_id to return execution with integer account ID
    orig_get = ExternalExecutionRepository.get_by_experiment_id

    def fake_get(self_repo: ExternalExecutionRepository, exp_id: UUID) -> ExternalExecution | None:
        rec = orig_get(self_repo, exp_id)
        if rec is not None:
            object.__setattr__(rec, "external_account_id", 1985595022114520)
        return rec

    monkeypatch.setattr(ExternalExecutionRepository, "get_by_experiment_id", fake_get)

    res = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.is_successful is False
    assert transport.campaign_pause_calls == 0
    assert "local account id '1985595022114520' is missing or malformed" in (res.rejection_reason or "").lower()


def test_52_campaign_ownership_rejects_unicode_numerals(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """52. Remote account ID with Unicode numerals blocks campaign pause."""
    transport = MockTransport(campaign_account_id="\u0661\u0662\u0663\u0664\u0665\u0666")
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
    assert transport.campaign_pause_calls == 0
    assert "missing or malformed" in (res.rejection_reason or "").lower()


def test_53_campaign_ownership_rejects_repeated_act_prefix(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """53. Remote account ID with repeated 'act_' prefixes blocks campaign pause."""
    transport = MockTransport(campaign_account_id="act_act_1985595022114520")
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
    assert transport.campaign_pause_calls == 0
    assert "missing or malformed" in (res.rejection_reason or "").lower()


def test_54_inspect_token_permanent_system_user_token_valid():
    """54. System user permanent token with expires_at == 0 passes inspection as VALID."""
    transport = MockTransport(token_never_expires=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    result = adapter.inspect_token(flight_duration_hours=48.0, safety_buffer_hours=24.0)
    assert result.status == TokenStatus.VALID
    assert result.is_valid is True
    assert result.expires_at is None
    assert result.remaining_seconds is None


def test_55_inspect_token_valid_long_lived_token():
    """55. Token valid for 90 days (> 72 hours) passes inspection as VALID."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    result = adapter.inspect_token(flight_duration_hours=48.0, safety_buffer_hours=24.0)
    assert result.status == TokenStatus.VALID
    assert result.is_valid is True
    assert result.remaining_seconds is not None
    assert result.remaining_seconds > 72.0 * 3600


def test_56_inspect_token_expired_token():
    """56. Token with expiration in the past returns EXPIRED status."""
    transport = MockTransport(token_expired=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    result = adapter.inspect_token(flight_duration_hours=48.0, safety_buffer_hours=24.0)
    assert result.status == TokenStatus.EXPIRED
    assert result.is_valid is False
    assert "Token expired at" in (result.rejection_reason or "")


def test_57_inspect_token_near_expiry_fails_preflight():
    """57. Token expiring in 20 hours (< 72 hours required) returns NEAR_EXPIRY."""
    transport = MockTransport(token_near_expiry=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    result = adapter.inspect_token(flight_duration_hours=48.0, safety_buffer_hours=24.0)
    assert result.status == TokenStatus.NEAR_EXPIRY
    assert result.is_valid is False
    assert "insufficient" in (result.rejection_reason or "").lower()
    assert "72.0h" in (result.rejection_reason or "")


def test_58_inspect_token_meta_rejected_invalid_token():
    """58. Meta reporting is_valid == False returns INVALID status."""
    transport = MockTransport(token_is_invalid=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    result = adapter.inspect_token()
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "Invalid OAuth access token" in (result.rejection_reason or "")


def test_59_inspect_token_missing_token_string():
    """59. Adapter with empty token string immediately returns INVALID."""
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token="", transport=MockTransport())

    result = adapter.inspect_token()
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "missing or empty" in (result.rejection_reason or "")


def test_60_inspect_token_network_api_error_fails_closed():
    """60. Network failure during token inspection fails closed with INSPECTION_UNAVAILABLE."""
    transport = MockTransport(fail_token_inspection=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    result = adapter.inspect_token()
    assert result.status == TokenStatus.INSPECTION_UNAVAILABLE
    assert result.is_valid is False
    assert "Token inspection request failed" in (result.rejection_reason or "")


def test_61_inspect_token_malformed_response_fails_closed():
    """61. Malformed JSON response without 'data' envelope fails closed with INSPECTION_UNAVAILABLE."""
    transport = MockTransport(malformed_token_response=True)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    result = adapter.inspect_token()
    assert result.status == TokenStatus.INSPECTION_UNAVAILABLE
    assert result.is_valid is False
    assert "Malformed debug_token response" in (result.rejection_reason or "")


def test_62_deploy_recovery_dormant_blocks_when_token_expired(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """62. deploy_recovery_dormant blocks immediately when access token is expired."""
    transport = MockTransport(token_expired=True)
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
    assert "TOKEN_PREFLIGHT_FAILED: EXPIRED" in res.reason


def test_63_deploy_recovery_dormant_blocks_when_token_near_expiry(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """63. deploy_recovery_dormant blocks when token expires within 72h (< 48h flight + 24h buffer)."""
    transport = MockTransport(token_near_expiry=True)
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
    assert "TOKEN_PREFLIGHT_FAILED: NEAR_EXPIRY" in res.reason


def test_64_activate_recovery_flight_blocks_when_token_expired(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """64. activate_recovery_flight blocks immediately when access token is expired."""
    # First deploy dormant with valid token
    transport_valid = MockTransport()
    adapter_valid = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_valid)
    deploy_res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter_valid,
        safe_mode=False,
        explicit_auth=True,
    )
    assert deploy_res.success is True

    # Now attempt activation with expired token
    transport_expired = MockTransport(token_expired=True)
    adapter_expired = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_expired)
    act_res = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter_expired,
        safe_mode=False,
        explicit_auth=True,
    )
    assert act_res.success is False
    assert act_res.blocked is True
    assert "TOKEN_PREFLIGHT_FAILED: EXPIRED" in act_res.reason


def test_65_activate_recovery_flight_blocks_when_token_near_expiry(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """65. activate_recovery_flight blocks when access token is near expiry (< 72h)."""
    # First deploy dormant with valid token
    transport_valid = MockTransport()
    adapter_valid = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_valid)
    deploy_res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter_valid,
        safe_mode=False,
        explicit_auth=True,
    )
    assert deploy_res.success is True

    # Attempt activation with near-expiry token
    transport_near = MockTransport(token_near_expiry=True)
    adapter_near = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport_near)
    act_res = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter_near,
        safe_mode=False,
        explicit_auth=True,
    )
    assert act_res.success is False
    assert act_res.blocked is True
    assert "TOKEN_PREFLIGHT_FAILED: NEAR_EXPIRY" in act_res.reason


def test_66_token_inspection_sanitizes_input_token_in_errors():
    """66. Error message containing input_token parameter redacts the secret value."""
    from venturebot.execution.dispatch import _sanitize_error

    err_str = "Error calling https://graph.facebook.com/v20.0/debug_token?input_token=EAAXsecret123&access_token=EAAXsecret456 with Bearer EAAXsecret789"
    cleaned = _sanitize_error(err_str)
    assert "EAAXsecret123" not in cleaned
    assert "EAAXsecret456" not in cleaned
    assert "EAAXsecret789" not in cleaned
    assert "input_token=[REDACTED]" in cleaned
    assert "access_token=[REDACTED]" in cleaned
    assert "Bearer [REDACTED]" in cleaned


def test_67_emergency_stop_routes_option_b_to_four_tier_pause(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """67. ExecutionDispatchService.emergency_stop routes Option B execution through 4-tier emergency_pause_adset."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Deploy Option B recovery (creates superseded lineage event)
    deploy_res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert deploy_res.success is True

    # Set mock remote objects to ACTIVE
    transport.ad_status = "ACTIVE"
    transport.adset_status = "ACTIVE"
    transport.original_adset_status = "ACTIVE"

    stop_res = ExecutionDispatchService.emergency_stop(
        session=session,
        experiment_id=approved_experiment.id,
        reason="Human operator triggered emergency stop.",
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert stop_res.is_successful is True
    # Proves all 4 tiers were inspected/paused
    assert stop_res.campaign_status is not None and stop_res.campaign_status.paused is True and stop_res.campaign_status.verified is True
    assert stop_res.ad_status is not None and stop_res.ad_status.paused is True and stop_res.ad_status.verified is True
    assert stop_res.replacement_adset_status is not None and stop_res.replacement_adset_status.paused is True and stop_res.replacement_adset_status.verified is True
    assert stop_res.original_adset_status is not None and stop_res.original_adset_status.paused is True and stop_res.original_adset_status.verified is True
    # Experiment marked KILLED
    assert stop_res.experiment is not None
    assert stop_res.experiment.status == ExperimentStatus.KILLED


def test_68_legacy_emergency_stop_prohibited_for_option_b(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """68. MetaExperimentDispatchService._emergency_stop rejects Option B recovery executions."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Deploy Option B recovery
    ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    # Directly invoking legacy _emergency_stop on Option B must fail closed
    legacy_res = MetaExperimentDispatchService._emergency_stop(
        session=session,
        experiment_id=approved_experiment.id,
        reason="Operator abort attempt",
        adapter=adapter,
    )
    assert legacy_res.is_successful is False
    assert "LEGACY_EMERGENCY_STOP_PROHIBITED_FOR_OPTION_B" in (legacy_res.rejection_reason or "")
    # Campaign was NOT paused via legacy method
    assert transport.campaign_pause_calls == 0


def test_69_emergency_stop_routes_non_option_b_to_legacy_stop(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
):
    """69. ExecutionDispatchService.emergency_stop routes standard non-Option B experiments to legacy stop."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Execution exists without any superseded ad set events (non-Option B)
    exec_repo = ExternalExecutionRepository(session)
    events = exec_repo.get_events(existing_execution.id)
    assert len(events) == 0

    stop_res = ExecutionDispatchService.emergency_stop(
        session=session,
        experiment_id=approved_experiment.id,
        reason="Operator triggered standard abort.",
        adapter=adapter,
    )
    assert stop_res.is_successful is True
    assert stop_res.experiment is not None
    assert stop_res.experiment.status == ExperimentStatus.KILLED
    assert transport.campaign_pause_calls == 1


def test_70_duplicate_recovery_ordinary_retry_adopts_existing_replacement(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """70. Retrying deploy_recovery_dormant with version=2 adopts existing replacement ad set."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # First deployment creates replacement
    r1 = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )
    assert r1.success is True

    # On retry with version 2:
    r2 = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )
    assert r2.success is True

    # Lineage check: superseded events must not be duplicated
    exec_repo = ExternalExecutionRepository(session)
    events = exec_repo.get_events(existing_execution.id)
    superseded = [e for e in events if e.event_type == ExternalExecutionEventType.RESOURCE_SUPERSEDED.value]
    assert len(superseded) == 1
    assert superseded[0].old_resource_id == ORIGINAL_ADSET_ID


def test_71_duplicate_recovery_unauthorized_version_change_blocked(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """71. Attempting version=3 without spec.authorized_recovery_version=3 is blocked."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Attempting to jump to version 3 without authorization
    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.success is False
    assert res.blocked is True
    assert "UNAUTHORIZED_RECOVERY_VERSION" in res.reason


def test_72_duplicate_recovery_untrusted_operator_identity_blocked(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """72. Version 3 is blocked because VentureBot lacks trusted operator authentication (Rules A.7/A.8)."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Durably record operator authorization event for recovery version 3
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_step90_12_9",
        reason="Explicit human approval for recovery version 3 retry",
    )

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.success is False
    assert res.blocked is True
    assert "UNTRUSTED_OPERATOR_AUTHENTICATION" in res.reason


def test_73_duplicate_recovery_invalid_version_below_two_blocked(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """73. Recovery version below 2 is rejected as INVALID_RECOVERY_VERSION."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=1,
    )
    assert res.success is False
    assert res.blocked is True
    assert "INVALID_RECOVERY_VERSION" in res.reason


# ── Section A: Inspector Authentication Verification (Tests 74–80) ────────────

def test_74_inspect_token_with_app_access_token_succeeds():
    """74. Token inspection with valid App Access Token succeeds and identifies APP_ACCESS_TOKEN."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        inspector_access_token="APP_ID|APP_SECRET",
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.VALID
    assert result.is_valid is True
    assert result.inspector_type == "APP_ACCESS_TOKEN"


def test_75_inspect_token_missing_inspector_fails_closed(monkeypatch: pytest.MonkeyPatch):
    """75. Missing inspector credential fails closed with UNVERIFIED_INSPECTOR_AUTHENTICATION."""
    monkeypatch.delenv("META_APP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("META_INSPECTOR_ACCESS_TOKEN", raising=False)
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        inspector_access_token=None,
        allow_self_inspection=False,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INSPECTION_UNAVAILABLE
    assert result.is_valid is False
    assert "UNVERIFIED_INSPECTOR_AUTHENTICATION" in (result.rejection_reason or "")


def test_76_inspect_token_self_inspection_allowed(monkeypatch: pytest.MonkeyPatch):
    """76. Explicitly allowing self-inspection identifies inspector_type as SELF_INSPECTION."""
    monkeypatch.delenv("META_APP_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("META_INSPECTOR_ACCESS_TOKEN", raising=False)
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        inspector_access_token=None,
        allow_self_inspection=True,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.VALID
    assert result.is_valid is True
    assert result.inspector_type == "SELF_INSPECTION"


def test_77_inspect_token_inspector_auth_rejected_by_meta():
    """77. Meta API error rejecting inspector credential (OAuthException 190) fails closed with INVALID."""
    transport = MockTransport(fail_inspector_auth=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "Meta rejected token" in (result.rejection_reason or "")


def test_78_inspect_token_inspector_wrong_app_rejected_by_meta():
    """78. Meta API rejecting inspector token permissions (OAuthException 10) fails closed."""
    transport = MockTransport(inspector_wrong_app=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "Meta rejected token" in (result.rejection_reason or "")


def test_79_inspect_token_network_failure_fails_closed():
    """79. Network failure during token inspection fails closed with INSPECTION_UNAVAILABLE."""
    transport = MockTransport(fail_token_inspection=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INSPECTION_UNAVAILABLE
    assert result.is_valid is False
    assert "Token inspection request failed" in (result.rejection_reason or "")


def test_80_inspect_token_malformed_response_fails_closed():
    """80. Malformed debug_token response missing 'data' envelope fails closed with INSPECTION_UNAVAILABLE."""
    transport = MockTransport(malformed_token_response=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INSPECTION_UNAVAILABLE
    assert result.is_valid is False
    assert "Malformed debug_token response" in (result.rejection_reason or "")


# ── Section B: Token Metadata Validation (Tests 81–88) ────────────────────────

def test_81_inspect_token_app_id_mismatch_rejected():
    """81. debug_token reporting app_id not matching expected app ID is rejected as INVALID."""
    transport = MockTransport(token_mismatched_app_id=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        expected_app_id="1063651013045060",
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "does not match expected" in (result.rejection_reason or "")


def test_82_inspect_token_incompatible_type_rejected():
    """82. debug_token reporting incompatible token type (e.g. PAGE) is rejected as INVALID."""
    transport = MockTransport(incompatible_token_type=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "Incompatible token type 'PAGE'" in (result.rejection_reason or "")


def test_83_inspect_token_missing_required_scopes_rejected():
    """83. debug_token missing required scopes ('ads_management', 'ads_read') is rejected as INVALID."""
    transport = MockTransport(missing_required_scopes=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "missing required permission scopes" in (result.rejection_reason or "")


def test_84_inspect_token_granular_scopes_unauthorized_ad_account_rejected():
    """84. Granular scopes restricting targets without the target ad account is rejected as INVALID."""
    transport = MockTransport(granular_scopes_unauthorized=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token(target_ad_account_id=SAMPLE_ACCOUNT_ID)
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "not authorized in granular_scopes" in (result.rejection_reason or "")


def test_85_inspect_token_granular_scopes_authorized_ad_account_succeeds():
    """85. Granular scopes explicitly containing the target ad account succeeds as VALID."""
    transport = MockTransport(granular_scopes_authorized=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token(target_ad_account_id=SAMPLE_ACCOUNT_ID)
    assert result.status == TokenStatus.VALID
    assert result.is_valid is True
    assert len(result.granular_scopes) > 0


def test_86_inspect_token_data_access_expired_rejected():
    """86. Token with data_access_expires_at in the past returns EXPIRED status."""
    transport = MockTransport(data_access_expired=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.EXPIRED
    assert result.is_valid is False
    assert "Data access expired" in (result.rejection_reason or "")


def test_87_inspect_token_data_access_near_expiry_rejected():
    """87. Token with data_access_expires_at within 72 hours returns NEAR_EXPIRY."""
    transport = MockTransport(data_access_near_expiry=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token(flight_duration_hours=48.0, safety_buffer_hours=24.0)
    assert result.status == TokenStatus.NEAR_EXPIRY
    assert result.is_valid is False
    assert "Data access remaining validity" in (result.rejection_reason or "")
    assert "Requires at least 72.0h" in (result.rejection_reason or "")


def test_88_inspect_token_permanent_token_requires_valid_metadata():
    """88. Permanent token (expires_at == 0) with mismatched app_id is rejected despite zero expiry."""
    transport = MockTransport(token_never_expires=True, token_mismatched_app_id=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )
    result = adapter.inspect_token()
    assert result.status == TokenStatus.INVALID
    assert result.is_valid is False
    assert "does not match expected" in (result.rejection_reason or "")


# ── Section C: Recovery-Version Authorization (Tests 89–92) ───────────────────

def test_89_record_recovery_version_authorization_persists_event(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
):
    """89. record_recovery_version_authorization creates immutable event with operator, version, and reason."""
    ev = ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_test_89",
        reason="Manual approval for version 3 test execution",
    )
    assert ev is not None
    assert ev.event_type == ExternalExecutionEventType.RECOVERY_VERSION_AUTHORIZED.value
    assert ev.execution_id == existing_execution.id

    # Verify event retrieval from repository
    repo = ExternalExecutionRepository(session)
    events = repo.get_events(existing_execution.id)
    auth_events = [e for e in events if e.event_type == ExternalExecutionEventType.RECOVERY_VERSION_AUTHORIZED.value]
    assert len(auth_events) == 1
    assert "operator_test_89" in (auth_events[0].reason or "")
    assert "version 3" in (auth_events[0].reason or "")


def test_90_deploy_recovery_dormant_v3_without_persisted_event_blocked(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """90. Caller setting spec.authorized_recovery_version=3 without persisted event is rejected."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # In-memory shortcut alone must NOT suffice
    valid_spec.authorized_recovery_version = 3

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.success is False
    assert res.blocked is True
    assert "UNAUTHORIZED_RECOVERY_VERSION" in res.reason


def test_91_deploy_recovery_dormant_v3_fails_closed_on_untrusted_operator(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """91. Persisting authorization event with caller-supplied identity fails closed (Rules A.7/A.8)."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="security_officer_42",
        reason="Authorization grant for version 3 dormant deployment",
    )

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.success is False
    assert res.blocked is True
    assert "UNTRUSTED_OPERATOR_AUTHENTICATION" in res.reason


def test_92_record_recovery_version_authorization_input_validation(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
):
    """92. record_recovery_version_authorization rejects invalid inputs (version < 3, empty operator, empty reason)."""
    # Invalid version < 3
    with pytest.raises(ValueError, match="authorized_version must be >= 3"):
        ExecutionDispatchService.record_recovery_version_authorization(
            session=session,
            execution_id=existing_execution.id,
            authorized_version=2,
            operator_id="operator_1",
            reason="valid reason",
        )

    # Empty operator
    with pytest.raises(ValueError, match="operator_id must be a non-empty string"):
        ExecutionDispatchService.record_recovery_version_authorization(
            session=session,
            execution_id=existing_execution.id,
            authorized_version=3,
            operator_id="   ",
            reason="valid reason",
        )

    # Empty reason
    with pytest.raises(ValueError, match="reason must be a non-empty string"):
        ExecutionDispatchService.record_recovery_version_authorization(
            session=session,
            execution_id=existing_execution.id,
            authorized_version=3,
            operator_id="operator_1",
            reason="",
        )


# ── Section D: Option B Emergency-Stop Classification (Tests 93–100) ──────────

def test_93_classify_execution_lineage_standard(
    session: Session,
    approved_experiment: ExperimentORM,
):
    """93. Execution with standard status and no superseded events is classified as STANDARD."""
    repo = ExternalExecutionRepository(session)
    exec_rec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176254130380",
            status=ExternalExecutionStatus.PENDING,
        )
    )
    events = repo.get_events(exec_rec.id)
    classification, orig_id, anomaly = classify_execution_lineage(exec_rec, events)
    assert classification == RecoveryLineageClassification.STANDARD
    assert orig_id is None
    assert anomaly is None


def test_94_classify_execution_lineage_option_b(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
    existing_execution: ExternalExecution,
):
    """94. Option B recovery execution with valid superseded event is classified as OPTION_B."""
    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    deploy_res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )
    assert deploy_res.success is True

    repo = ExternalExecutionRepository(session)
    exec_rec = repo.get_by_id(existing_execution.id)
    assert exec_rec is not None
    events = repo.get_events(exec_rec.id)

    classification, orig_id, anomaly = classify_execution_lineage(exec_rec, events)
    assert classification == RecoveryLineageClassification.OPTION_B
    assert orig_id == ORIGINAL_ADSET_ID
    assert anomaly is None


def test_95_classify_execution_lineage_dormant_recovery_missing_event_ambiguous(
    session: Session,
    approved_experiment: ExperimentORM,
):
    """95. DORMANT_RECOVERY execution missing RESOURCE_SUPERSEDED event classifies as AMBIGUOUS_RECOVERY."""
    repo = ExternalExecutionRepository(session)
    exec_rec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176254130380",
            status=ExternalExecutionStatus.DORMANT_RECOVERY,
        )
    )
    events = repo.get_events(exec_rec.id)
    classification, orig_id, anomaly = classify_execution_lineage(exec_rec, events)
    assert classification == RecoveryLineageClassification.AMBIGUOUS_RECOVERY
    assert orig_id is None
    assert "RESOURCE_SUPERSEDED lineage event is missing" in (anomaly or "")


def test_96_classify_execution_lineage_malformed_old_id_ambiguous(
    session: Session,
    approved_experiment: ExperimentORM,
):
    """96. Superseded event with empty or malformed old_resource_id classifies as AMBIGUOUS_RECOVERY."""
    repo = ExternalExecutionRepository(session)
    exec_rec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176254130380",
            status=ExternalExecutionStatus.DORMANT_RECOVERY,
        )
    )
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=exec_rec.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="adset",
            old_resource_id="",
            new_resource_id="120252176254130399",
            reason="Superseded",
        )
    )
    events = repo.get_events(exec_rec.id)
    classification, orig_id, anomaly = classify_execution_lineage(exec_rec, events)
    assert classification == RecoveryLineageClassification.AMBIGUOUS_RECOVERY
    assert "old_resource_id is missing or empty" in (anomaly or "")


def test_97_classify_execution_lineage_conflicting_old_ids_ambiguous(
    session: Session,
    approved_experiment: ExperimentORM,
):
    """97. Multiple superseded events with conflicting old_resource_id classifies as AMBIGUOUS_RECOVERY."""
    repo = ExternalExecutionRepository(session)
    exec_rec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176254130380",
            status=ExternalExecutionStatus.DORMANT_RECOVERY,
        )
    )
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=exec_rec.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="adset",
            old_resource_id="120252176254130380",
            new_resource_id="120252176254130399",
            reason="First superseding",
        )
    )
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=exec_rec.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="adset",
            old_resource_id="999999999999999999",
            new_resource_id="120252176254130399",
            reason="Conflicting superseding",
        )
    )
    events = repo.get_events(exec_rec.id)
    classification, orig_id, anomaly = classify_execution_lineage(exec_rec, events)
    assert classification == RecoveryLineageClassification.AMBIGUOUS_RECOVERY
    assert "distinct old_resource_ids" in (anomaly or "")


def test_98_classify_execution_lineage_replacement_matches_original_ambiguous(
    session: Session,
    approved_experiment: ExperimentORM,
):
    """98. Replacement ad set ID identical to original ad set ID classifies as AMBIGUOUS_RECOVERY."""
    repo = ExternalExecutionRepository(session)
    exec_rec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176254130380",
            status=ExternalExecutionStatus.DORMANT_RECOVERY,
        )
    )
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=exec_rec.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="adset",
            old_resource_id="120252176254130380",
            new_resource_id="120252176254130380",  # Same ID!
            reason="Cyclic superseding",
        )
    )
    events = repo.get_events(exec_rec.id)
    classification, orig_id, anomaly = classify_execution_lineage(exec_rec, events)
    assert classification == RecoveryLineageClassification.AMBIGUOUS_RECOVERY
    assert "matches superseded original adset_id" in (anomaly or "")


def test_99_emergency_stop_ambiguous_lineage_fails_closed_never_routes_legacy(
    session: Session,
    approved_experiment: ExperimentORM,
):
    """99. ExecutionDispatchService.emergency_stop fails closed on AMBIGUOUS_RECOVERY and makes zero mutation calls."""
    repo = ExternalExecutionRepository(session)
    exec_rec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176254130380",
            status=ExternalExecutionStatus.DORMANT_RECOVERY,
        )
    )
    # No superseded events added -> triggers AMBIGUOUS_RECOVERY

    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    stop_res = ExecutionDispatchService.emergency_stop(
        session=session,
        experiment_id=approved_experiment.id,
        reason="Test abort on corrupted execution lineage",
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert stop_res.is_successful is False
    assert "AMBIGUOUS_RECOVERY_LINEAGE" in (stop_res.rejection_reason or "")
    # Proves campaign was NEVER touched via legacy path
    assert transport.campaign_pause_calls == 0


def test_100_legacy_emergency_stop_rejects_ambiguous_recovery_lineage(
    session: Session,
    approved_experiment: ExperimentORM,
):
    """100. MetaExperimentDispatchService._emergency_stop fails closed when invoked on AMBIGUOUS_RECOVERY lineage."""
    repo = ExternalExecutionRepository(session)
    exec_rec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176254130380",
            status=ExternalExecutionStatus.DORMANT_RECOVERY,
        )
    )

    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    legacy_res = MetaExperimentDispatchService._emergency_stop(
        session=session,
        experiment_id=approved_experiment.id,
        reason="Attempting direct legacy emergency stop on ambiguous recovery lineage",
        adapter=adapter,
    )
    assert legacy_res.is_successful is False
    assert "AMBIGUOUS_RECOVERY_LINEAGE_PROHIBITED" in (legacy_res.rejection_reason or "")
    assert transport.campaign_pause_calls == 0


def test_101_approval_for_execution_a_cannot_authorize_execution_b(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """101. Approval for execution A cannot authorize execution B."""
    repo = ExternalExecutionRepository(session)
    # Create second execution B under a different experiment
    exp_b = ExperimentORM(
        id=uuid4(),
        opportunity_id=approved_experiment.opportunity_id,
        hypothesis="Exp B",
        objective="Validate demand B",
        channel="facebook",
        monetization_method="direct_sale",
        destination_url="https://example.com/b",
        status=ExperimentStatus.APPROVED.value,
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("200.00"),
        actual_spend=Decimal("0.00"),
        success_criteria="CTR >= 1.5%",
        failure_criteria="CTR < 0.5%",
    )
    session.add(exp_b)
    session.flush()

    exec_b = repo.create(
        ExternalExecution(
            experiment_id=exp_b.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id="120252176243860399",
            adset_id="120252176254130399",
            status=ExternalExecutionStatus.FAILED,
        )
    )

    # Record authorization strictly for execution A
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_valid",
        reason="Approval for execution A",
    )

    # Attempt deployment for execution B with version 3
    spec_b = MetaExecutionSpecification(
        experiment_id=exp_b.id,
        ad_account_id=SAMPLE_ACCOUNT_ID,
        page_id="1389949167526709",
        destination_url="https://example.com/b",
        primary_text="Text",
        headline="Headline",
        image_hash=SAMPLE_IMAGE_HASH,
        call_to_action="LEARN_MORE",
        campaign_objective="OUTCOME_TRAFFIC",
        countries=["IN"],
        age_min=22,
        age_max=55,
        authorized_budget=Decimal("200.00"),
        start_time=valid_spec.start_time,
        end_time=valid_spec.end_time,
        special_ad_categories=["NONE"],
        status="PAUSED",
        explicit_dispatch_authorized=True,
    )

    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=exp_b.id,
        spec=spec_b,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.success is False
    assert res.blocked is True
    assert "UNAUTHORIZED_RECOVERY_VERSION" in res.reason


def test_102_approval_for_experiment_a_cannot_authorize_experiment_b(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """102. Approval for experiment A cannot authorize experiment B."""
    wrong_exp_id = uuid4()
    with pytest.raises(ValueError, match="does not match experiment"):
        ExecutionDispatchService.record_recovery_version_authorization(
            session=session,
            execution_id=existing_execution.id,
            experiment_id=wrong_exp_id,
            authorized_version=3,
            operator_id="operator_sec",
            reason="Mismatched IDs",
        )


def test_103_approval_for_version_3_cannot_authorize_version_4(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """103. Approval for version 3 cannot authorize version 4."""
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_test",
        reason="Authorize version 3 strictly",
    )

    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=4,
    )
    assert res.success is False
    assert res.blocked is True
    assert "Approval for version 3 cannot authorize version 4" in res.reason


def test_104_duplicate_authorization_event_rejected(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
):
    """104. Duplicate authorization attempt for the same version and execution raises ValueError."""
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_first",
        reason="First authorization",
    )

    with pytest.raises(ValueError, match="Duplicate authorization rejected"):
        ExecutionDispatchService.record_recovery_version_authorization(
            session=session,
            execution_id=existing_execution.id,
            authorized_version=3,
            operator_id="operator_second",
            reason="Second duplicate authorization",
        )


def test_105_conflicting_authorization_event_rejected(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
):
    """105. Conflicting authorization attempt for a different version on the same execution raises ValueError."""
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_v3",
        reason="Authorize version 3",
    )

    with pytest.raises(ValueError, match="Conflicting authorization rejected"):
        ExecutionDispatchService.record_recovery_version_authorization(
            session=session,
            execution_id=existing_execution.id,
            authorized_version=4,
            operator_id="operator_v4",
            reason="Attempt conflicting version 4",
        )


def test_106_replay_prevention_rejects_repeated_deployment_after_partial_creation(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """106. Replay prevention: Authorization cannot silently authorize repeated resource creation."""
    repo = ExternalExecutionRepository(session)
    # Record authorization for version 3
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_replay_test",
        reason="Replay test authorization",
    )

    # Simulate that replacement resource for version 3 was already created
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=existing_execution.id,
            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
            tier="adset",
            old_resource_id=None,
            new_resource_id="120252176299999999-ADSET-v3",
            reason="Simulated prior creation of replacement adset",
        )
    )

    transport = MockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.success is False
    assert res.blocked is True
    assert "RECOVERY_AUTHORIZATION_REPLAY_REJECTED" in res.reason


def test_107_duplicate_superseded_events_classified_as_ambiguous(
    session: Session,
    approved_experiment: ExperimentORM,
):
    """107. Duplicate superseded events with same resource ID classifies as AMBIGUOUS_RECOVERY."""
    repo = ExternalExecutionRepository(session)
    exec_rec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176254130380",
            status=ExternalExecutionStatus.DORMANT_RECOVERY,
        )
    )
    # Add two identical superseded events (duplicate mutation records)
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=exec_rec.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="adset",
            old_resource_id="120252176254130380",
            new_resource_id="120252176254130399",
            reason="First superseding event",
        )
    )
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=exec_rec.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="adset",
            old_resource_id="120252176254130380",
            new_resource_id="120252176254130399",
            reason="Duplicate superseding event",
        )
    )

    events = repo.get_events(exec_rec.id)
    classification, orig_id, anomaly = classify_execution_lineage(exec_rec, events)
    assert classification == RecoveryLineageClassification.AMBIGUOUS_RECOVERY
    assert "Duplicate or conflicting superseded events detected" in (anomaly or "")
