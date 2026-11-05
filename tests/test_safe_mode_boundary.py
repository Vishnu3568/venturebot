"""Tests for Adapter-Level SAFE_MODE Enforcement & Test Harness Isolation (Step 90.12.14).

Covers Task C:
1. SAFE_MODE enabled + configured transport + POST: rejected before transport call.
2. SAFE_MODE enabled + configured transport + PUT: rejected before transport call.
3. SAFE_MODE enabled + configured transport + DELETE: rejected before transport call.
4. SAFE_MODE enabled + configured transport + GET: allowed.
5. SAFE_MODE disabled + ordinary write without required authorization: rejected by dispatch authorization.
6. Direct calls to all adapter write methods cannot bypass SAFE_MODE.
7. Caller-supplied safe_mode=False cannot override the environment guard.
8. Test harness bypass cannot be activated through ordinary production environment configuration.
9. Emergency-stop policy: emergency pause is disabled under SAFE_MODE and cannot authorize create/activate.

Asserts that configured fake transport receives zero write requests whenever the guard blocks.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import os
from pathlib import Path
import sys
from typing import Any
import urllib.request
from uuid import UUID, uuid4

# Ensure test directory is in sys.path for test helper imports
_test_dir = str(Path(__file__).resolve().parent)
if _test_dir not in sys.path:
    sys.path.insert(0, _test_dir)

import pytest
from sqlalchemy.orm import Session

from test_meta_recovery_option_b import (
    ORIGINAL_ADSET_ID,
    SAMPLE_ACCOUNT_ID,
    SAMPLE_CAMPAIGN_ID,
    SAMPLE_IMAGE_HASH,
    SAMPLE_TOKEN,
    valid_spec,
    session,
    approved_experiment,
    existing_execution,
)
from venturebot.database.models import ExperimentORM, ExternalExecutionORM
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.env import is_safe_mode
import venturebot.execution.dispatch as dispatch_module
from venturebot.execution.dispatch import (
    ExecutionAction,
    ExecutionDispatchResult,
    ExecutionDispatchService,
    ExecutionRequest,
    MetaExperimentDispatchService,
    is_safe_mode_enforced,
)
from venturebot.execution.meta import (
    MetaApiError,
    MetaApiSafetyError,
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
)
from venturebot.models.external_execution import (
    ExternalExecution,
    ExternalExecutionStatus,
)


class RecordingFakeTransport:
    """Configured fake transport that records every HTTP request attempted."""

    def __init__(self, response_bytes: bytes = b'{"success": true, "id": "123"}') -> None:
        self.calls: list[dict[str, Any]] = []
        self.response_bytes = response_bytes

    def __call__(self, req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        data = req.data.decode("utf-8") if req.data else ""
        self.calls.append({
            "method": req.get_method(),
            "url": req.full_url,
            "data": data,
            "headers": dict(req.headers),
        })
        return self.response_bytes

    @property
    def write_count(self) -> int:
        return len([c for c in self.calls if c["method"].upper() in ("POST", "PUT", "DELETE", "PATCH")])

    @property
    def read_count(self) -> int:
        return len([c for c in self.calls if c["method"].upper() == "GET"])


# ==============================================================================
# Task C.1: SAFE_MODE enabled + configured transport + POST rejected before call
# ==============================================================================

def test_safe_mode_enabled_configured_transport_post_rejected_before_call(
    monkeypatch: pytest.MonkeyPatch,
):
    """1. SAFE_MODE enabled + configured transport + POST: rejected before transport call."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")
    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    with pytest.raises(MetaApiSafetyError, match="while VENTUREBOT_SAFE_MODE is active"):
        adapter.pause_campaign(SAMPLE_CAMPAIGN_ID)

    # Transport must receive ZERO calls
    assert transport.write_count == 0
    assert len(transport.calls) == 0


# ==============================================================================
# Task C.2: SAFE_MODE enabled + configured transport + PUT rejected before call
# ==============================================================================

def test_safe_mode_enabled_configured_transport_put_rejected_before_call(
    monkeypatch: pytest.MonkeyPatch,
):
    """2. SAFE_MODE enabled + configured transport + PUT: rejected before transport call."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")
    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    req = urllib.request.Request(
        f"https://graph.facebook.com/v20.0/{SAMPLE_CAMPAIGN_ID}",
        data=b'{"name": "Updated Name"}',
        method="PUT",
    )

    with pytest.raises(MetaApiSafetyError, match="while VENTUREBOT_SAFE_MODE is active"):
        adapter._execute_request(req)

    assert transport.write_count == 0
    assert len(transport.calls) == 0


# ==============================================================================
# Task C.3: SAFE_MODE enabled + configured transport + DELETE rejected before call
# ==============================================================================

def test_safe_mode_enabled_configured_transport_delete_rejected_before_call(
    monkeypatch: pytest.MonkeyPatch,
):
    """3. SAFE_MODE enabled + configured transport + DELETE: rejected before transport call."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")
    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    req = urllib.request.Request(
        f"https://graph.facebook.com/v20.0/{SAMPLE_CAMPAIGN_ID}",
        method="DELETE",
    )

    with pytest.raises(MetaApiSafetyError, match="while VENTUREBOT_SAFE_MODE is active"):
        adapter._execute_request(req)

    assert transport.write_count == 0
    assert len(transport.calls) == 0


# ==============================================================================
# Task C.4: SAFE_MODE enabled + configured transport + GET allowed
# ==============================================================================

def test_safe_mode_enabled_configured_transport_get_allowed(
    monkeypatch: pytest.MonkeyPatch,
):
    """4. SAFE_MODE enabled + configured transport + GET: allowed."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")
    payload = b'{"id": "act_1985595022114520", "name": "Test Account", "account_status": 1, "currency": "INR"}'
    transport = RecordingFakeTransport(response_bytes=payload)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    meta = adapter.get_account_metadata("1985595022114520")
    assert meta.id == "act_1985595022114520"
    assert meta.is_active is True
    assert meta.currency == "INR"

    # GET was executed through the transport
    assert transport.read_count == 1
    assert transport.write_count == 0


# ==============================================================================
# Task C.5: SAFE_MODE disabled + ordinary write without authorization rejected
# ==============================================================================

def test_safe_mode_disabled_ordinary_write_without_auth_rejected(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecutionORM,
    valid_spec: MetaExecutionSpecification,
    monkeypatch: pytest.MonkeyPatch,
):
    """5. SAFE_MODE disabled + ordinary write without required authorization: rejected by dispatch."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "false")
    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    # A. activate_recovery_flight without explicit_auth fails
    res_act = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
    )
    assert res_act.success is False
    assert "AUTHORIZATION" in (res_act.reason or "")

    # B. deploy_recovery_dormant without explicit_auth fails
    res_dep = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
    )
    assert res_dep.success is False
    assert "AUTHORIZATION" in (res_dep.reason or "")

    # C. emergency_pause_adset without explicit_auth fails
    res_pause = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
    )
    assert res_pause.is_successful is False
    assert "AUTHORIZATION" in (res_pause.rejection_reason or "")

    # Transport received zero write requests
    assert transport.write_count == 0


# ==============================================================================
# Task C.6: Direct calls to all adapter write methods cannot bypass SAFE_MODE
# ==============================================================================

def test_direct_calls_to_all_adapter_write_methods_cannot_bypass_safe_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    """6. Direct calls to all adapter write methods cannot bypass SAFE_MODE."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")
    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    now_utc = datetime.now(timezone.utc)

    # 1. pause_campaign
    with pytest.raises(MetaApiSafetyError):
        adapter.pause_campaign(SAMPLE_CAMPAIGN_ID)

    # 2. unpause_campaign
    with pytest.raises(MetaApiSafetyError):
        adapter.unpause_campaign(SAMPLE_CAMPAIGN_ID)

    # 3. pause_adset
    with pytest.raises(MetaApiSafetyError):
        adapter.pause_adset(ORIGINAL_ADSET_ID)

    # 4. unpause_adset
    with pytest.raises(MetaApiSafetyError):
        adapter.unpause_adset(ORIGINAL_ADSET_ID)

    # 5. pause_ad
    with pytest.raises(MetaApiSafetyError):
        adapter.pause_ad("120252176222222222")

    # 6. unpause_ad
    with pytest.raises(MetaApiSafetyError):
        adapter.unpause_ad("120252176222222222")

    # 7. create_campaign
    with pytest.raises(MetaApiSafetyError):
        adapter.create_campaign("VB-EXP-TEST", "OUTCOME_TRAFFIC")

    # 8. create_adset
    with pytest.raises(MetaApiSafetyError):
        adapter.create_adset("120", "VB-EXP-TEST-ADSET", 10000, now_utc + timedelta(hours=48), now_utc)

    # 9. create_creative
    with pytest.raises(MetaApiSafetyError):
        adapter.create_creative("VB-EXP-CR", "1389949167526709", "https://example.com", "msg", "hl", "hash")

    # 10. create_ad
    with pytest.raises(MetaApiSafetyError):
        adapter.create_ad("120", "VB-EXP-AD", "creative_123")

    # 11. update_adset_schedule
    with pytest.raises(MetaApiSafetyError):
        adapter.update_adset_schedule(ORIGINAL_ADSET_ID, now_utc + timedelta(hours=24), now_utc + timedelta(hours=72))

    # Transport received ZERO write requests
    assert transport.write_count == 0
    assert len(transport.calls) == 0


# ==============================================================================
# Task C.7: Caller-supplied safe_mode=False cannot override environment guard
# ==============================================================================

def test_caller_supplied_safe_mode_false_cannot_override_environment_guard(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecutionORM,
    valid_spec: MetaExecutionSpecification,
    monkeypatch: pytest.MonkeyPatch,
):
    """7. Caller-supplied safe_mode=False cannot override the environment guard."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")
    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    # Direct function verification
    assert is_safe_mode_enforced(caller_safe_mode=False) is True

    # A. deploy_recovery_dormant with safe_mode=False
    res_deploy = ExecutionDispatchService.deploy_recovery_dormant(
        session=session,
        experiment_id=approved_experiment.id,
        spec=valid_spec,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
        version=2,
    )
    assert res_deploy.success is False
    assert res_deploy.blocked is True
    assert "SAFE_MODE_ENABLED" in (res_deploy.reason or "")

    # B. activate_recovery_flight with safe_mode=False
    res_activate = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_activate.success is False
    assert res_activate.blocked is True
    assert "SAFE_MODE_ENABLED" in (res_activate.reason or "")

    # C. lock_schedule with safe_mode=False
    now_utc = datetime.now(timezone.utc)
    res_lock = ExecutionDispatchService.lock_schedule(
        session=session,
        experiment_id=approved_experiment.id,
        target_start=now_utc + timedelta(hours=24),
        target_end=now_utc + timedelta(hours=72),
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_lock.success is False
    assert res_lock.blocked is True
    assert "SAFE_MODE_ENABLED" in (res_lock.reason or "")

    # D. dispatch with safe_mode=False
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT.value,
        proposed_budget=Decimal("100.00"),
    )
    res_dispatch = ExecutionDispatchService.dispatch(
        request=req,
        session=session,
        experiment=approved_experiment,
        safe_mode=False,
        spec=valid_spec,
        adapter=adapter,
    )
    assert res_dispatch.success is False
    assert res_dispatch.blocked is True
    assert "SAFE_MODE_ENABLED" in res_dispatch.reason

    # Transport received zero write requests
    assert transport.write_count == 0


# ==============================================================================
# Task C.8: Test harness bypass cannot be activated via production configuration
# ==============================================================================

def test_test_harness_bypass_cannot_be_activated_through_production_config(
    monkeypatch: pytest.MonkeyPatch,
):
    """8. Test harness bypass cannot be activated through ordinary production environment configuration."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")

    # A. _TestHarnessContext must NOT exist in production dispatch module
    assert not hasattr(dispatch_module, "_TestHarnessContext")

    # B. tests/conftest.py must NOT exist
    conftest_path = Path(_test_dir) / "conftest.py"
    assert not conftest_path.exists()

    # C. Arbitrary test bypass environment variables cannot disable safe mode
    monkeypatch.setenv("TEST_HARNESS_ACTIVE", "true")
    monkeypatch.setenv("VENTUREBOT_TEST_HARNESS", "1")
    monkeypatch.setenv("VENTUREBOT_BYPASS_SAFE_MODE", "true")
    assert is_safe_mode() is True
    assert is_safe_mode_enforced(caller_safe_mode=False) is True

    # D. Invalid environment string fails closed
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "any_invalid_string")
    assert is_safe_mode() is True
    assert is_safe_mode_enforced(caller_safe_mode=False) is True


# ==============================================================================
# Task C.9: Emergency-stop policy under SAFE_MODE & inability to authorize create/activate
# ==============================================================================

def test_emergency_stop_policy_under_safe_mode(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecutionORM,
    monkeypatch: pytest.MonkeyPatch,
):
    """9. Emergency-stop policy: emergency pause is disabled under SAFE_MODE and cannot authorize create/activate."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")
    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    # A. Under SAFE_MODE=true, emergency_pause_adset is blocked fail-closed (Task E policy)
    res_pause = ExecutionDispatchService.emergency_pause_adset(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=True,
    )
    assert res_pause.is_successful is False
    assert "SAFE_MODE_ENABLED" in (res_pause.rejection_reason or "")
    assert transport.write_count == 0

    # B. Legacy emergency stop is also blocked under SAFE_MODE=true
    res_legacy = MetaExperimentDispatchService._emergency_stop(
        session=session,
        experiment_id=approved_experiment.id,
        reason="Emergency stop in safe mode",
        adapter=adapter,
    )
    assert res_legacy.is_successful is False
    assert "SAFE_MODE_ENABLED" in (res_legacy.rejection_reason or "")
    assert transport.write_count == 0

    # C. Under SAFE_MODE=true, activation is strictly blocked by SAFE_MODE
    res_act = ExecutionDispatchService.activate_recovery_flight(
        session=session,
        experiment_id=approved_experiment.id,
        adapter=adapter,
        safe_mode=False,
        explicit_auth=False,
    )
    assert res_act.success is False
    assert res_act.blocked is True
    assert "SAFE_MODE_ENABLED" in (res_act.reason or "")
    assert transport.write_count == 0
