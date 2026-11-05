"""Tests for Granular Meta Deployment Architecture (Step 90.15.2).

Verifies the decomposition of monolithic Option B recovery deployment into
independently controlled, auditable, and idempotent tiers:
Tier 1: deploy_recovery_adset_dormant()   -> PARTIAL_ADSET
Tier 2: deploy_recovery_creative_dormant() -> PARTIAL_CREATIVE
Tier 3: deploy_recovery_ad_dormant()       -> DEPLOYED_DORMANT

Covers all 13 isolation, boundary, idempotency, and failure recovery requirements.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import threading
from typing import Any
import urllib.request
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from venturebot.database.models import (
    Base,
    CapitalTransactionORM,
    DeploymentClaimORM,
    ExperimentORM,
    ExternalExecutionEventORM,
    ExternalExecutionORM,
    OpportunityORM,
)
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.execution.dispatch import (
    ExecutionDispatchService,
    RecoveryDormantResult,
)
from venturebot.execution.meta import (
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
    TokenStatus,
    deterministic_ad_name,
    deterministic_adset_name,
    deterministic_campaign_name,
    deterministic_creative_name,
)
from venturebot.models.capital import TransactionType
from venturebot.models.experiment import ExperimentStatus
from venturebot.models.external_execution import (
    ExternalExecution,
    ExternalExecutionEvent,
    ExternalExecutionEventType,
    ExternalExecutionStatus,
)

SAMPLE_ACCOUNT_ID = "act_1985595022114520"
SAMPLE_TOKEN = "TEST_SYSTEM_USER_TOKEN_SAFE"
SAMPLE_APP_TOKEN = "TEST_APP_ACCESS_TOKEN_SAFE"
SAMPLE_APP_ID = "1063651013045060"
SAMPLE_CAMPAIGN_ID = "120252176243860380"
ORIGINAL_ADSET_ID = "120252176254130380"
SAMPLE_IMAGE_HASH = "d921604e0240ee8329b3b7fe5235abd4"


@pytest.fixture(autouse=True)
def _configure_test_env(monkeypatch: pytest.MonkeyPatch):
    """Ensure safe testing environment variables."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "false")
    monkeypatch.setenv("META_APP_ACCESS_TOKEN", SAMPLE_APP_TOKEN)
    monkeypatch.setenv("META_APP_ID", SAMPLE_APP_ID)
    monkeypatch.setenv("META_ACCESS_TOKEN", SAMPLE_TOKEN)


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
            last_error="Error creating creative: Meta API HTTP 400 error",
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


class GranularMockTransport:
    """Mock transport that simulates Meta Graph API responses and tracks granular endpoint calls."""

    def __init__(
        self,
        cbo_enabled: bool = False,
        existing_adset: dict[str, Any] | None = None,
        existing_creative: dict[str, Any] | None = None,
        existing_ad: dict[str, Any] | None = None,
    ):
        self.requests: list[urllib.request.Request] = []
        self.cbo_enabled = cbo_enabled
        self.existing_adset = existing_adset
        self.existing_creative = existing_creative
        self.existing_ad = existing_ad

        self.adset_create_calls: list[dict[str, Any]] = []
        self.creative_create_calls: list[dict[str, Any]] = []
        self.ad_create_calls: list[dict[str, Any]] = []

    def __call__(self, req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        self.requests.append(req)
        url = req.full_url
        data_body = req.data.decode("utf-8") if req.data else ""

        # /debug_token
        if "/debug_token" in url and req.get_method() == "GET":
            now_ts = int(datetime.now(timezone.utc).timestamp())
            return json.dumps({
                "data": {
                    "app_id": SAMPLE_APP_ID,
                    "type": "SYSTEM_USER",
                    "application": "VentureBot",
                    "data_access_expires_at": now_ts + (90 * 86400),
                    "expires_at": now_ts + (90 * 86400),
                    "is_valid": True,
                    "scopes": ["ads_management", "ads_read"],
                    "granular_scopes": [
                        {"scope": "ads_management", "target_ids": [SAMPLE_ACCOUNT_ID.removeprefix("act_")]},
                        {"scope": "ads_read", "target_ids": [SAMPLE_ACCOUNT_ID.removeprefix("act_")]},
                    ],
                    "user_id": "987654321",
                }
            }).encode("utf-8")

        # CBO check
        if "is_adset_budget_sharing_enabled" in url:
            return json.dumps({
                "id": SAMPLE_CAMPAIGN_ID,
                "is_adset_budget_sharing_enabled": self.cbo_enabled,
                "budget_rebalance_flag": False,
            }).encode("utf-8")

        # Campaign GET
        if (f"/{SAMPLE_CAMPAIGN_ID}?" in url or url.endswith(f"/{SAMPLE_CAMPAIGN_ID}")) and "/adsets" not in url and req.get_method() == "GET":
            return json.dumps({
                "id": SAMPLE_CAMPAIGN_ID,
                "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f",
                "status": "PAUSED",
                "effective_status": "PAUSED",
                "account_id": SAMPLE_ACCOUNT_ID.removeprefix("act_"),
            }).encode("utf-8")

        # Ad set lookup GET
        if "/adsets?fields=" in url and req.get_method() == "GET":
            if self.existing_adset:
                return json.dumps({"data": [self.existing_adset]}).encode("utf-8")
            return json.dumps({"data": []}).encode("utf-8")

        # Ad set create POST
        if "/adsets" in url and req.data:
            payload = json.loads(data_body)
            self.adset_create_calls.append(payload)
            self.existing_adset = {
                "id": "120252176299999999",
                "name": payload.get("name"),
                "status": payload.get("status", "PAUSED"),
                "lifetime_budget": str(payload.get("lifetime_budget", "20000")),
                "campaign_id": SAMPLE_CAMPAIGN_ID,
            }
            return json.dumps({"id": "120252176299999999"}).encode("utf-8")

        # Creative lookup GET
        if "/adcreatives?fields=" in url and req.get_method() == "GET":
            if self.existing_creative:
                return json.dumps({"data": [self.existing_creative]}).encode("utf-8")
            return json.dumps({"data": []}).encode("utf-8")

        # Creative create POST
        if "/adcreatives" in url and req.data:
            payload = json.loads(data_body)
            self.creative_create_calls.append(payload)
            self.existing_creative = {
                "id": "creative_rec_999",
                "name": payload.get("name"),
                "object_story_spec": payload.get("object_story_spec", {}),
            }
            return json.dumps({"id": "creative_rec_999"}).encode("utf-8")

        # Ad lookup GET
        if "/ads?fields=" in url and req.get_method() == "GET":
            if self.existing_ad:
                return json.dumps({"data": [self.existing_ad]}).encode("utf-8")
            return json.dumps({"data": []}).encode("utf-8")

        # Ad create POST
        if "/ads" in url and req.data:
            payload = json.loads(data_body)
            self.ad_create_calls.append(payload)
            self.existing_ad = {
                "id": "ad_rec_888",
                "name": payload.get("name"),
                "status": payload.get("status", "PAUSED"),
                "adset_id": payload.get("adset_id"),
                "creative": {"id": payload.get("creative", {}).get("creative_id")},
            }
            return json.dumps({"id": "ad_rec_888"}).encode("utf-8")

        return json.dumps({"success": True}).encode("utf-8")


# ── 1. Ad Set Only Creates Only Ad Set, Never Creative or Ad ─────────────────

def test_adset_only_creates_only_adset_never_creative_or_ad(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """1. Tier 1 creates replacement ad set; creative and ad creation are NEVER called."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )

    assert res.success is True
    assert res.blocked is False
    assert res.adset_id == "120252176299999999"
    assert res.creative_id is None
    assert res.ad_id is None

    # Verify adapter endpoint isolation
    assert len(transport.adset_create_calls) == 1
    assert len(transport.creative_create_calls) == 0
    assert len(transport.ad_create_calls) == 0

    # Verify payload properties
    created_payload = transport.adset_create_calls[0]
    assert created_payload["name"] == "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2"
    assert created_payload["status"] == "PAUSED"
    assert created_payload["lifetime_budget"] == 20000

    # Verify database persistence
    repo = ExternalExecutionRepository(session)
    updated = repo.get_by_id(existing_execution.id)
    assert updated is not None
    assert updated.adset_id == "120252176299999999"
    assert updated.creative_id is None
    assert updated.ad_id is None
    assert updated.status == ExternalExecutionStatus.PARTIAL_ADSET.value


# ── 2. Ad Set Only Adopts Existing Ad Set on Idempotency Lookup ───────────────

def test_adset_only_adopts_existing_adset_on_idempotency_lookup(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """2. Tier 1 adopts existing replacement ad set on lookup without duplicate creation."""
    existing_adset_data = {
        "id": "120252176288888888",
        "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2",
        "status": "PAUSED",
        "lifetime_budget": "20000",
        "campaign_id": SAMPLE_CAMPAIGN_ID,
    }
    transport = GranularMockTransport(existing_adset=existing_adset_data)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )

    assert res.success is True
    assert res.adset_id == "120252176288888888"
    # Zero creations occurred because it adopted existing
    assert len(transport.adset_create_calls) == 0

    repo = ExternalExecutionRepository(session)
    events = repo.get_events(existing_execution.id)
    adopted_events = [
        e for e in events
        if e.event_type == ExternalExecutionEventType.RESOURCE_ADOPTED.value and e.tier == "adset"
    ]
    assert len(adopted_events) == 1
    assert adopted_events[0].new_resource_id == "120252176288888888"


# ── 3. Ad Set Only Sets PARTIAL_ADSET Status, Never DEPLOYED_DORMANT ─────────

def test_adset_only_sets_partial_adset_status_never_deployed_dormant(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """3. Tier 1 rigorously asserts status is PARTIAL_ADSET and never DEPLOYED_DORMANT."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )
    assert res.success is True

    repo = ExternalExecutionRepository(session)
    updated = repo.get_by_id(existing_execution.id)
    assert updated is not None
    assert updated.status == ExternalExecutionStatus.PARTIAL_ADSET.value
    assert updated.status != ExternalExecutionStatus.DEPLOYED_DORMANT.value


# ── 4. Creative Only Requires Persisted adset_id ─────────────────────────────

def test_creative_only_requires_persisted_adset_id(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """4. Tier 2 fails closed with MISSING_ADSET_PREREQUISITE if adset_id is missing."""
    repo = ExternalExecutionRepository(session)
    # Execution with NO adset_id
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id=None,
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PENDING,
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    assert "MISSING_ADSET_PREREQUISITE" in res.reason
    assert len(transport.creative_create_calls) == 0


# ── 5. Creative Only Creates Only Creative, Never Ad ─────────────────────────

def test_creative_only_creates_only_creative_never_ad(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """5. Tier 2 creates creative; ad and adset creation endpoints are NEVER called."""
    repo = ExternalExecutionRepository(session)
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id=None,
            ad_id=None,
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PARTIAL_ADSET,
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is True
    assert res.creative_id == "creative_rec_999"
    assert res.ad_id is None

    # Endpoint isolation
    assert len(transport.creative_create_calls) == 1
    assert len(transport.adset_create_calls) == 0
    assert len(transport.ad_create_calls) == 0

    # Persistence
    updated = repo.get_by_id(execution.id)
    assert updated is not None
    assert updated.adset_id == "120252176299999999"
    assert updated.creative_id == "creative_rec_999"
    assert updated.ad_id is None
    assert updated.status == ExternalExecutionStatus.PARTIAL_CREATIVE.value


# ── 6. Creative Only Sets PARTIAL_CREATIVE Status ─────────────────────────────

def test_creative_only_sets_partial_creative_status(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """6. Tier 2 rigorously asserts status is PARTIAL_CREATIVE and never DEPLOYED_DORMANT."""
    repo = ExternalExecutionRepository(session)
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PARTIAL_ADSET,
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.success is True

    updated = repo.get_by_id(execution.id)
    assert updated is not None
    assert updated.status == ExternalExecutionStatus.PARTIAL_CREATIVE.value
    assert updated.status != ExternalExecutionStatus.DEPLOYED_DORMANT.value


# ── 7. Ad Only Requires Persisted adset_id and creative_id ───────────────────

def test_ad_only_requires_persisted_adset_and_creative_ids(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """7. Tier 3 fails closed if either adset_id or creative_id is missing."""
    repo = ExternalExecutionRepository(session)
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Case A: Missing adset_id
    exec_no_adset = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id=None,
            creative_id="creative_rec_999",
            status=ExternalExecutionStatus.PARTIAL_CREATIVE,
        )
    )

    res_a = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_a.success is False
    assert res_a.blocked is True
    assert "MISSING_ADSET_PREREQUISITE" in res_a.reason
    assert len(transport.ad_create_calls) == 0

    # Test Case B: Missing creative_id
    exec_no_adset.adset_id = "120252176299999999"
    exec_no_adset.creative_id = None
    repo.save(exec_no_adset)

    res_b = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_b.success is False
    assert res_b.blocked is True
    assert "MISSING_CREATIVE_PREREQUISITE" in res_b.reason
    assert len(transport.ad_create_calls) == 0


# ── 8. Ad Only Creates Only Ad in PAUSED Status ───────────────────────────────

def test_ad_only_creates_only_ad_in_paused_status(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """8. Tier 3 creates ad in PAUSED status; adset and creative endpoints are NEVER called."""
    repo = ExternalExecutionRepository(session)
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id="creative_rec_999",
            ad_id=None,
            status=ExternalExecutionStatus.PARTIAL_CREATIVE,
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is True
    assert res.ad_id == "ad_rec_888"

    # Isolation
    assert len(transport.ad_create_calls) == 1
    assert len(transport.adset_create_calls) == 0
    assert len(transport.creative_create_calls) == 0

    # Status must be PAUSED
    ad_payload = transport.ad_create_calls[0]
    assert ad_payload["status"] == "PAUSED"
    assert ad_payload["adset_id"] == "120252176299999999"
    assert ad_payload["creative"] == {"creative_id": "creative_rec_999"}


# ── 9. Ad Only Advances Status to DEPLOYED_DORMANT ────────────────────────────

def test_ad_only_advances_status_to_deployed_dormant(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """9. Tier 3 successfully advances execution status to DEPLOYED_DORMANT."""
    repo = ExternalExecutionRepository(session)
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id="creative_rec_999",
            ad_id=None,
            status=ExternalExecutionStatus.PARTIAL_CREATIVE,
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res.success is True

    updated = repo.get_by_id(execution.id)
    assert updated is not None
    assert updated.status == ExternalExecutionStatus.DEPLOYED_DORMANT.value
    assert updated.ad_id == "ad_rec_888"


# ── 10. SAFE_MODE Enforced Across All Granular Tiers ─────────────────────────

def test_safe_mode_enforced_across_all_granular_tiers(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """10. All three granular tiers fail closed with SAFE_MODE_ENABLED when safe_mode is True."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Tier 1 with safe_mode=True
    res1 = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=True,
        explicit_auth=True,
    )
    assert res1.success is False
    assert res1.blocked is True
    assert res1.reason == "SAFE_MODE_ENABLED"

    # Tier 2 with safe_mode=True
    res2 = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=True,
        explicit_auth=True,
    )
    assert res2.success is False
    assert res2.blocked is True
    assert res2.reason == "SAFE_MODE_ENABLED"

    # Tier 3 with safe_mode=True
    res3 = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=True,
        explicit_auth=True,
    )
    assert res3.success is False
    assert res3.blocked is True
    assert res3.reason == "SAFE_MODE_ENABLED"

    # Zero mutations executed on any tier
    assert len(transport.adset_create_calls) == 0
    assert len(transport.creative_create_calls) == 0
    assert len(transport.ad_create_calls) == 0


# ── 11. Untrusted Operator Identity Blocked on Granular Adset ─────────────────

def test_untrusted_operator_identity_blocked_on_granular_adset(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """11. Tier 1 fails closed on version 3 with UNTRUSTED_OPERATOR_AUTHENTICATION (Rules A.7/A.8)."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_granular_test",
        reason="Authorize version 3 for granular test",
    )

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
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
    assert len(transport.adset_create_calls) == 0


# ── 12. Replay Protection on Granular Adset ───────────────────────────────────

def test_replay_protection_on_granular_adset(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """12. Tier 1 rejects replay attempts when replacement resource for version already exists."""
    repo = ExternalExecutionRepository(session)
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        execution_id=existing_execution.id,
        authorized_version=3,
        operator_id="operator_replay",
        reason="Authorization for replay test",
    )

    # Simulate existing created event for version 3
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=existing_execution.id,
            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
            tier="adset",
            old_resource_id=None,
            new_resource_id="120252176299999999-ADSET-v3",
            reason="Prior created replacement ad set for version 3",
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
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
    assert len(transport.adset_create_calls) == 0


# ── 13. Monolithic deploy_recovery_dormant Composes All Three Tiers ───────────

def test_monolithic_deploy_recovery_dormant_composes_all_three_tiers(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """13. Composite deploy_recovery_dormant() executes Tier 1 -> Tier 2 -> Tier 3 in sequence."""
    transport = GranularMockTransport()
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

    # All three creations occurred in sequence
    assert len(transport.adset_create_calls) == 1
    assert len(transport.creative_create_calls) == 1
    assert len(transport.ad_create_calls) == 1

    # End-state is DEPLOYED_DORMANT
    repo = ExternalExecutionRepository(session)
    updated = repo.get_by_id(existing_execution.id)
    assert updated is not None
    assert updated.status == ExternalExecutionStatus.DEPLOYED_DORMANT.value
    assert updated.adset_id == "120252176299999999"
    assert updated.creative_id == "creative_rec_999"
    assert updated.ad_id == "ad_rec_888"


# ── 14. Activated State Blocks All Granular Tiers ──────────────────────────────

def test_activated_state_blocks_all_granular_tiers(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """14. Activated executions cannot be re-executed across any tier."""
    repo = ExternalExecutionRepository(session)
    activated_exec = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id="creative_rec_999",
            ad_id="ad_rec_888",
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.ACTIVATED,
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Tier 1 blocked
    r1 = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r1.success is False
    assert r1.blocked is True
    assert "already in live/activated state" in r1.reason

    # Tier 2 blocked
    r2 = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r2.success is False
    assert r2.blocked is True
    assert "already in live/activated state" in r2.reason

    # Tier 3 blocked
    r3 = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r3.success is False
    assert r3.blocked is True
    assert "already in live/activated state" in r3.reason


# ── 15. State Machine Prerequisite Enforcement Across Tiers ──────────────────

def test_tier2_rejects_unrecovered_failed_execution_state(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """15. Tier 2 fails closed if called on Step 90 partial state (FAILED status with expired adset_id)."""
    # existing_execution has status=FAILED and adset_id=ORIGINAL_ADSET_ID
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    assert "INVALID_PREREQUISITE_STATE" in res.reason
    assert len(transport.creative_create_calls) == 0


def test_tier3_rejects_partial_adset_execution_state(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """16. Tier 3 fails closed if called on PARTIAL_ADSET status before Tier 2 has created creative."""
    repo = ExternalExecutionRepository(session)
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id=None,
            ad_id=None,
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PARTIAL_ADSET,
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    # Fails closed on missing creative prerequisite
    assert "MISSING_CREATIVE_PREREQUISITE" in res.reason or "INVALID_PREREQUISITE_STATE" in res.reason
    assert len(transport.ad_create_calls) == 0


# ── 16. Financial Spend & Budget Ceiling Enforcement in Tier 2 and Tier 3 ─────

def test_tier2_and_tier3_enforce_zero_spend(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """17. Both Tier 2 and Tier 3 fail closed with ACTUAL_SPEND_EXCEEDS_ZERO if spend > 0."""
    from venturebot.database.models import CapitalTransactionORM
    from venturebot.models.capital import TransactionType

    repo = ExternalExecutionRepository(session)
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id="creative_rec_999",
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PARTIAL_CREATIVE,
        )
    )

    # Record actual spend on capital ledger
    session.add(
        CapitalTransactionORM(
            id=uuid4(),
            transaction_type=TransactionType.EXPERIMENT_SPEND.value,
            amount=Decimal("15.00"),
            experiment_id=approved_experiment.id,
            description="Prior spend on experiment",
        )
    )
    session.commit()

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Tier 2 blocked
    r2 = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r2.success is False
    assert r2.blocked is True
    assert "ACTUAL_SPEND_EXCEEDS_ZERO" in r2.reason

    # Tier 3 blocked
    r3 = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r3.success is False
    assert r3.blocked is True
    assert "ACTUAL_SPEND_EXCEEDS_ZERO" in r3.reason


def test_tier2_and_tier3_enforce_budget_limits(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """18. Both Tier 2 and Tier 3 reject unauthorized budget ceilings."""
    repo = ExternalExecutionRepository(session)
    repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id="creative_rec_999",
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PARTIAL_CREATIVE,
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Spec requesting ₹300.00 > ₹200.00 allocated
    over_budget_spec = valid_spec.model_copy()
    over_budget_spec.authorized_budget = Decimal("300.00")

    r2 = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=over_budget_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r2.success is False
    assert r2.blocked is True
    assert "AUTHORIZED_BUDGET_EXCEEDS_ALLOCATION" in r2.reason

    r3 = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=over_budget_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r3.success is False
    assert r3.blocked is True
    assert "AUTHORIZED_BUDGET_EXCEEDS_ALLOCATION" in r3.reason


# ── 17. Idempotency Status Verification (PAUSED Status Enforced) ─────────────

def test_tier1_rejects_adopting_active_replacement_adset(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """19. Tier 1 rejects adopting an existing replacement ad set if its status is ACTIVE."""
    active_adset_data = {
        "id": "120252176288888888",
        "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2",
        "status": "ACTIVE",  # DANGEROUS! Not PAUSED
        "lifetime_budget": "20000",
        "campaign_id": SAMPLE_CAMPAIGN_ID,
    }
    transport = GranularMockTransport(existing_adset=active_adset_data)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )

    assert res.success is False
    assert res.blocked is True
    assert "UNEXPECTED_RESOURCE_STATUS" in res.reason
    assert "ACTIVE" in res.reason


def test_tier3_rejects_adopting_active_ad(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """20. Tier 3 rejects adopting an existing ad if its status is ACTIVE."""
    repo = ExternalExecutionRepository(session)
    repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id="creative_rec_999",
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PARTIAL_CREATIVE,
        )
    )

    active_ad_data = {
        "id": "ad_rec_active",
        "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-AD",
        "status": "ACTIVE",  # DANGEROUS! Not PAUSED
        "adset_id": "120252176299999999",
        "creative": {"id": "creative_rec_999"},
    }
    transport = GranularMockTransport(existing_ad=active_ad_data)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert res.success is False
    assert res.blocked is True
    assert "UNEXPECTED_RESOURCE_STATUS" in res.reason
    assert "ACTIVE" in res.reason


# ── 18. Failure-Recovery Scenarios (A, B, C, D, E) ─────────────────────────────

def test_tier1_failure_recovery_crash_before_db_persists_retry_adopts(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """21. Failure recovery (Scenario A/B/C): If Meta created ad set but client crashed/timed out before DB update, retry adopts it."""
    # First attempt: Meta created ad set "120252176299999999", but process crashed before execution.adset_id was saved
    # Next attempt simulates that ad set now exists remotely on Meta in PAUSED status
    remote_adset_data = {
        "id": "120252176299999999",
        "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2",
        "status": "PAUSED",
        "lifetime_budget": "20000",
        "campaign_id": SAMPLE_CAMPAIGN_ID,
    }
    transport = GranularMockTransport(existing_adset=remote_adset_data)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    retry_res = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )

    assert retry_res.success is True
    assert retry_res.adset_id == "120252176299999999"
    # Adopts existing without calling create endpoint
    assert len(transport.adset_create_calls) == 0

    repo = ExternalExecutionRepository(session)
    updated = repo.get_by_id(existing_execution.id)
    assert updated is not None
    assert updated.adset_id == "120252176299999999"
    assert updated.status == ExternalExecutionStatus.PARTIAL_ADSET.value


def test_tier2_failure_recovery_crash_before_db_persists_retry_adopts(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """22. Failure recovery (Scenario A/B/C): If Meta created creative but client crashed/timed out before DB update, retry adopts it."""
    repo = ExternalExecutionRepository(session)
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id=None,  # DB was not updated due to crash
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PARTIAL_ADSET,
        )
    )

    remote_creative_data = {
        "id": "creative_rec_999",
        "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-CREATIVE",
        "object_story_spec": {
            "page_id": "1389949167526709",
            "link_data": {
                "image_hash": SAMPLE_IMAGE_HASH,
                "link": "https://vishnu3568.github.io/venturebot/pilot/freelance-workflow/",
            },
        },
    }
    transport = GranularMockTransport(existing_creative=remote_creative_data)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    retry_res = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert retry_res.success is True
    assert retry_res.creative_id == "creative_rec_999"
    assert len(transport.creative_create_calls) == 0

    updated = repo.get_by_id(execution.id)
    assert updated is not None
    assert updated.creative_id == "creative_rec_999"
    assert updated.status == ExternalExecutionStatus.PARTIAL_CREATIVE.value


def test_tier3_failure_recovery_crash_before_db_persists_retry_adopts(
    session: Session,
    approved_experiment: ExperimentORM,
    valid_spec: MetaExecutionSpecification,
):
    """23. Failure recovery (Scenario A/B/C): If Meta created ad but client crashed/timed out before DB update, retry adopts it."""
    repo = ExternalExecutionRepository(session)
    execution = repo.create(
        ExternalExecution(
            experiment_id=approved_experiment.id,
            channel="meta",
            external_account_id=SAMPLE_ACCOUNT_ID,
            campaign_id=SAMPLE_CAMPAIGN_ID,
            adset_id="120252176299999999",
            creative_id="creative_rec_999",
            ad_id=None,  # DB was not updated due to crash
            image_hash=SAMPLE_IMAGE_HASH,
            status=ExternalExecutionStatus.PARTIAL_CREATIVE,
        )
    )

    remote_ad_data = {
        "id": "ad_rec_888",
        "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-AD",
        "status": "PAUSED",
        "adset_id": "120252176299999999",
        "creative": {"id": "creative_rec_999"},
    }
    transport = GranularMockTransport(existing_ad=remote_ad_data)
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    retry_res = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )

    assert retry_res.success is True
    assert retry_res.ad_id == "ad_rec_888"
    assert len(transport.ad_create_calls) == 0

    updated = repo.get_by_id(execution.id)
    assert updated is not None
    assert updated.ad_id == "ad_rec_888"
    assert updated.status == ExternalExecutionStatus.DEPLOYED_DORMANT.value


def test_concurrency_race_condition_second_attempt_adopts_cleanly(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """24. Concurrency (Scenario E): Second attempt adopts existing resource without duplicate creation."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Attempt 1 creates ad set
    r1 = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )
    assert r1.success is True
    assert len(transport.adset_create_calls) == 1

    # Simulate that transport now sees the created resource for concurrent/subsequent attempt 2
    transport.existing_adset = {
        "id": "120252176299999999",
        "name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v2",
        "status": "PAUSED",
        "lifetime_budget": "20000",
        "campaign_id": SAMPLE_CAMPAIGN_ID,
    }

    # Attempt 2 runs
    r2 = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )
    assert r2.success is True
    # Still only 1 create call occurred across both attempts!
    assert len(transport.adset_create_calls) == 1
    assert r2.adset_id == "120252176299999999"


# ── Step 90.15.4: True Concurrency Barrier Tests & Operator Auth Tests ───────

def _init_concurrent_db(
    tmp_path: Path,
    opp_id: UUID,
    experiment_id: UUID,
    execution: ExternalExecution,
) -> sessionmaker[Session]:
    """Helper to initialize an isolated, thread-safe SQLite WAL database for multi-threaded testing."""
    db_file = tmp_path / f"concurrent_{uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"timeout": 30.0})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL;"))
        conn.commit()
    Base.metadata.create_all(bind=engine)
    sm = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sm() as s:
        opp = OpportunityORM(
            id=opp_id,
            title="Solopreneur Financial Workflow Guide",
            category="freelance",
            status="approved",
            description="Problem validation pilot",
        )
        s.add(opp)
        s.flush()
        exp = ExperimentORM(
            id=experiment_id,
            opportunity_id=opp_id,
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
        s.add(exp)
        s.commit()
        repo = ExternalExecutionRepository(s, auto_commit=True)
        repo.create(execution)
    return sm


def test_true_concurrent_adset_creation_barrier(
    tmp_path: Path,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """25. Part B: True concurrent ad set creation with threading.Barrier(2).

    Two parallel threads concurrently invoke deploy_recovery_adset_dormant.
    The synchronization barrier ensures both threads run simultaneously.
    claim_deployment_tier ensures only one thread performs creation; the other
    double-checks and adopts the resource. Exactly 1 Meta create call occurs.
    """
    ThreadSession = _init_concurrent_db(
        tmp_path,
        approved_experiment.opportunity_id,
        approved_experiment.id,
        existing_execution,
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    barrier = threading.Barrier(2)
    results: list[RecoveryDormantResult | None] = [None, None]
    errors: list[Exception | None] = [None, None]

    def worker(idx: int):
        t_session = ThreadSession()
        try:
            barrier.wait(timeout=5.0)
            res = ExecutionDispatchService.deploy_recovery_adset_dormant(
                session=t_session,
                experiment_id=approved_experiment.id,
                spec=valid_spec,
                adapter=adapter,
                safe_mode=False,
                explicit_auth=True,
                version=2,
            )
            results[idx] = res
        except Exception as e:
            errors[idx] = e
        finally:
            t_session.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert errors[0] is None, f"Thread 0 error: {errors[0]}"
    assert errors[1] is None, f"Thread 1 error: {errors[1]}"
    assert results[0] is not None and results[0].success is True
    assert results[1] is not None and results[1].success is True
    assert results[0].adset_id == "120252176299999999"
    assert results[1].adset_id == "120252176299999999"

    # Exactly ONE remote create call was executed
    assert len(transport.adset_create_calls) == 1


def test_true_concurrent_creative_creation_barrier(
    tmp_path: Path,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """26. Part B: True concurrent creative creation with threading.Barrier(2).

    Two parallel threads concurrently invoke deploy_recovery_creative_dormant.
    Exactly 1 Meta create_creative call occurs; second thread adopts cleanly.
    """
    existing_execution.adset_id = "120252176299999999"
    existing_execution.status = ExternalExecutionStatus.PARTIAL_ADSET
    existing_execution.creative_id = None
    ThreadSession = _init_concurrent_db(
        tmp_path,
        approved_experiment.opportunity_id,
        approved_experiment.id,
        existing_execution,
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    barrier = threading.Barrier(2)
    results: list[RecoveryDormantResult | None] = [None, None]
    errors: list[Exception | None] = [None, None]

    def worker(idx: int):
        t_session = ThreadSession()
        try:
            barrier.wait(timeout=5.0)
            res = ExecutionDispatchService.deploy_recovery_creative_dormant(
                session=t_session,
                experiment_id=approved_experiment.id,
                spec=valid_spec,
                adapter=adapter,
                safe_mode=False,
                explicit_auth=True,
            )
            results[idx] = res
        except Exception as e:
            errors[idx] = e
        finally:
            t_session.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert errors[0] is None, f"Thread 0 error: {errors[0]}"
    assert errors[1] is None, f"Thread 1 error: {errors[1]}"
    assert results[0] is not None and results[0].success is True
    assert results[1] is not None and results[1].success is True
    assert results[0].creative_id == "creative_rec_999"
    assert results[1].creative_id == "creative_rec_999"

    # Exactly ONE remote create call was executed
    assert len(transport.creative_create_calls) == 1


def test_true_concurrent_ad_creation_barrier(
    tmp_path: Path,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """27. Part B: True concurrent ad creation with threading.Barrier(2).

    Two parallel threads concurrently invoke deploy_recovery_ad_dormant.
    Exactly 1 Meta create_ad call occurs; second thread adopts cleanly.
    """
    existing_execution.adset_id = "120252176299999999"
    existing_execution.creative_id = "creative_rec_999"
    existing_execution.status = ExternalExecutionStatus.PARTIAL_CREATIVE
    existing_execution.ad_id = None
    ThreadSession = _init_concurrent_db(
        tmp_path,
        approved_experiment.opportunity_id,
        approved_experiment.id,
        existing_execution,
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    barrier = threading.Barrier(2)
    results: list[RecoveryDormantResult | None] = [None, None]
    errors: list[Exception | None] = [None, None]

    def worker(idx: int):
        t_session = ThreadSession()
        try:
            barrier.wait(timeout=5.0)
            res = ExecutionDispatchService.deploy_recovery_ad_dormant(
                session=t_session,
                experiment_id=approved_experiment.id,
                spec=valid_spec,
                adapter=adapter,
                safe_mode=False,
                explicit_auth=True,
            )
            results[idx] = res
        except Exception as e:
            errors[idx] = e
        finally:
            t_session.close()

    t1 = threading.Thread(target=worker, args=(0,))
    t2 = threading.Thread(target=worker, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert errors[0] is None, f"Thread 0 error: {errors[0]}"
    assert errors[1] is None, f"Thread 1 error: {errors[1]}"
    assert results[0] is not None and results[0].success is True
    assert results[1] is not None and results[1].success is True
    assert results[0].ad_id == "ad_rec_888"
    assert results[1].ad_id == "ad_rec_888"

    # Exactly ONE remote create call was executed
    assert len(transport.ad_create_calls) == 1


def test_abandoned_deployment_claim_auto_reclaimed_after_ttl(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """28. Part B: Abandoned claim from crashed process is safely purged after TTL."""
    # Seed an expired claim in DB from a simulated crashed worker
    expired_claim = DeploymentClaimORM(
        id=uuid4(),
        experiment_id=approved_experiment.id,
        tier="adset",
        claim_token="stale_token_from_crashed_pid",
        claimed_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=4),
    )
    session.add(expired_claim)
    session.commit()

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
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


def test_unauthenticated_local_caller_cannot_mutate_meta_resources(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """29. Part D.1: Unauthenticated local caller with explicit_auth=False is rejected across all tiers."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Tier 1
    r1 = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
    )
    assert r1.blocked is True
    assert r1.reason == "EXPLICIT_AUTHORIZATION_REQUIRED"

    # Tier 2
    r2 = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
    )
    assert r2.blocked is True
    assert r2.reason == "EXPLICIT_AUTHORIZATION_REQUIRED"

    # Tier 3
    r3 = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
    )
    assert r3.blocked is True
    assert r3.reason == "EXPLICIT_AUTHORIZATION_REQUIRED"

    # Composite
    rc = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
    )
    assert rc.blocked is True
    assert rc.reason == "EXPLICIT_AUTHORIZATION_REQUIRED"

    # Zero remote Meta mutations occurred
    assert len(transport.adset_create_calls) == 0
    assert len(transport.creative_create_calls) == 0
    assert len(transport.ad_create_calls) == 0


def test_explicit_auth_alone_is_insufficient_for_higher_versions(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """30. Part D.2: explicit_auth=True alone is insufficient for recovery versions > 2."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.blocked is True
    assert "UNAUTHORIZED_RECOVERY_VERSION" in (res.reason or "")
    assert "Caller-supplied parameter alone is rejected" in (res.reason or "")
    assert len(transport.adset_create_calls) == 0


def test_caller_supplied_operator_id_does_not_constitute_authentication(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """31. Part D.3: Passing an arbitrary operator_id string does not authenticate the caller."""
    # Seed an audit event containing a caller-supplied operator_id for version 3
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        experiment_id=approved_experiment.id,
        version=3,
        operator_id="security_admin_bob",
        reason="Caller attempting manual override with arbitrary operator identity",
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.blocked is True
    assert "UNTRUSTED_OPERATOR_AUTHENTICATION" in (res.reason or "")
    assert "security_admin_bob" in (res.reason or "")
    assert len(transport.adset_create_calls) == 0


def test_each_tier_enforces_authorization_independently(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """32. Part D.4: Each tier independently validates budget, spend, and prerequisites."""
    # 1. Spend check: insert a spend transaction
    cap_tx = CapitalTransactionORM(
        id=uuid4(),
        experiment_id=approved_experiment.id,
        amount=Decimal("15.00"),
        transaction_type=TransactionType.EXPERIMENT_SPEND.value,
        description="Simulated spend",
        recorded_at=datetime.now(timezone.utc),
    )
    session.add(cap_tx)
    session.commit()

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Tier 2 blocked by spend
    r2 = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r2.blocked is True
    assert r2.reason == "ACTUAL_SPEND_EXCEEDS_ZERO"

    # Tier 3 blocked by spend
    r3 = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r3.blocked is True
    assert r3.reason == "ACTUAL_SPEND_EXCEEDS_ZERO"

    # Remove spend
    session.delete(cap_tx)
    session.commit()

    # 2. Prerequisite check: Tier 2 without adset_id
    repo = ExternalExecutionRepository(session, auto_commit=True)
    existing_execution.adset_id = None
    existing_execution.status = ExternalExecutionStatus.FAILED
    repo.save(existing_execution)

    r2_pre = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r2_pre.blocked is True
    assert "MISSING_ADSET_PREREQUISITE" in (r2_pre.reason or "")

    # Prerequisite check: Tier 3 without creative_id
    existing_execution.adset_id = "120252176299999999"
    existing_execution.creative_id = None
    existing_execution.status = ExternalExecutionStatus.PARTIAL_ADSET
    repo.save(existing_execution)

    r3_pre = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert r3_pre.blocked is True
    assert "MISSING_CREATIVE_PREREQUISITE" in (r3_pre.reason or "")


def test_composite_execution_cannot_bypass_tier_authorization(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """33. Part D.5: Composite deployment cannot bypass tier-level authorization."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Blocked by untrusted operator auth on version 3
    res_v3 = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res_v3.blocked is True
    assert ("UNAUTHORIZED_RECOVERY_VERSION" in (res_v3.reason or "") or "UNTRUSTED_OPERATOR_AUTHENTICATION" in (res_v3.reason or ""))

    # Blocked by explicit_auth=False
    res_noauth = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
        version=2,
    )
    assert res_noauth.blocked is True
    assert res_noauth.reason == "EXPLICIT_AUTHORIZATION_REQUIRED"


def test_replayed_recovery_authorization_is_rejected(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """34. Part D.6: Replayed recovery authorization is rejected."""
    # Seed an approval event for version 3
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        experiment_id=approved_experiment.id,
        version=3,
        operator_id="admin_alice",
        reason="Prior operator authorization",
    )

    repo = ExternalExecutionRepository(session, auto_commit=True)
    # Record event showing version 3 replacement ad set was already created
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=existing_execution.id,
            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
            tier="adset",
            old_resource_id=None,
            new_resource_id="120252176299999999",
            reason="Created dormant replacement ad set (VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v3) with 24h buffer schedule",
            api_request_summary=json.dumps({"name": "VB-EXP-49fde874-9387-5056-934c-51a9cfca164f-ADSET-v3", "version": 3}),
        )
    )

    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    res = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=3,
    )
    assert res.blocked is True
    assert "RECOVERY_AUTHORIZATION_REPLAY_REJECTED" in (res.reason or "")


def test_safe_mode_blocks_mutations_unconditionally_regardless_of_auth(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecution,
    valid_spec: MetaExecutionSpecification,
):
    """35. Part D.7: SAFE_MODE blocks mutations unconditionally regardless of explicit_auth."""
    transport = GranularMockTransport()
    adapter = MetaMarketingApiAdapter(ad_account_id=SAMPLE_ACCOUNT_ID, access_token=SAMPLE_TOKEN, transport=transport)

    # Tier 1
    r1 = ExecutionDispatchService.deploy_recovery_adset_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=True,
        explicit_auth=True,
        version=2,
    )
    assert r1.blocked is True
    assert r1.reason == "SAFE_MODE_ENABLED"

    # Tier 2
    r2 = ExecutionDispatchService.deploy_recovery_creative_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=True,
        explicit_auth=True,
    )
    assert r2.blocked is True
    assert r2.reason == "SAFE_MODE_ENABLED"

    # Tier 3
    r3 = ExecutionDispatchService.deploy_recovery_ad_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=True,
        explicit_auth=True,
    )
    assert r3.blocked is True
    assert r3.reason == "SAFE_MODE_ENABLED"

    # Composite
    rc = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=True,
        explicit_auth=True,
        version=2,
    )
    assert rc.blocked is True
    assert rc.reason == "SAFE_MODE_ENABLED"

    assert len(transport.requests) == 0


