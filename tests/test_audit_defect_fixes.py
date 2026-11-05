"""Regression tests for Step 90.12.15 Independent Code Audit Defect Fixes.

Covers:
1. Replay prevention in deploy_recovery_dormant for versions > 2 with realistic numeric Meta IDs in new_resource_id.
2. Token inspection granular_scopes matching with whitespace-padded and prefixed account IDs, and malformed account rejection.
3. Strict MetaMarketingApiAdapter.normalize_ad_account_id and MetaExecutionSpecification validation (rejecting act_act_, punctuation, letters).
4. MetaExperimentDispatchService._emergency_stop enforcement of caller-supplied safe_mode=True under VENTUREBOT_SAFE_MODE=false.
5. sanitize_error credential redaction across query parameters, Bearer tokens, and JSON payloads (including appsecret_proof).
6. classify_execution_lineage recognition of versioned ad set events when execution.adset_id is purely numeric.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any
import urllib.request
from uuid import uuid4

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
    MockTransport,
    approved_experiment,
    existing_execution,
    session,
    valid_spec,
)

SAMPLE_PAGE_ID = "109876543210123"


class RecordingFakeTransport:
    """Configured fake transport recording attempted requests."""

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
from venturebot.database.models import ExperimentORM, ExternalExecutionORM
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.execution.dispatch import (
    ExecutionDispatchService,
    MetaExperimentDispatchService,
    RecoveryLineageClassification,
    classify_execution_lineage,
)
from venturebot.execution.meta import (
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
    TokenStatus,
    sanitize_error,
    validate_meta_account_id,
)
from venturebot.models.external_execution import (
    ExternalExecution,
    ExternalExecutionEvent,
    ExternalExecutionEventType,
    ExternalExecutionStatus,
)


# ==============================================================================
# 1. Replay Prevention with Realistic Numeric Meta IDs in Production Lineage
# ==============================================================================

def test_recovery_replay_prevention_with_realistic_numeric_resource_ids(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecutionORM,
    valid_spec: MetaExecutionSpecification,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify that replay prevention works when new_resource_id is a numeric Meta ID."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "false")
    repo = ExternalExecutionRepository(session)

    # Durable authorization for version 3
    ExecutionDispatchService.record_recovery_version_authorization(
        session=session,
        experiment_id=approved_experiment.id,
        version=3,
        operator_id="operator_test",
        reason="Testing replay prevention fix",
    )

    # First deployment: simulates prior creation where new_resource_id is the numeric Meta ID
    # and version is captured in api_request_summary or reason
    repo.record_event(
        ExternalExecutionEvent(
            execution_id=existing_execution.id,
            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
            tier="adset",
            old_resource_id=None,
            new_resource_id="120252176299999999",  # purely numeric Meta ID!
            reason="Created dormant replacement ad set (VB-EXP-test-ADSET-v3) with 24h buffer schedule",
            api_request_summary=json.dumps({"name": "VB-EXP-test-ADSET-v3", "version": 3}),
        )
    )

    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    # Second invocation of deploy_recovery_dormant with version=3 must be blocked as replay
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
    assert "RECOVERY_AUTHORIZATION_REPLAY_REJECTED" in (res.reason or "")
    assert transport.write_count == 0


# ==============================================================================
# 2. Token Inspection Account ID Normalization and Whitespace Handling
# ==============================================================================

def test_inspect_token_granular_scopes_handles_whitespace_and_act_prefix():
    """Verify that inspect_token matches granular_scopes even with whitespace and act_ prefixes."""
    transport = MockTransport(granular_scopes_authorized=True)
    adapter = MetaMarketingApiAdapter(
        ad_account_id="1985595022114520",
        access_token=SAMPLE_TOKEN,
        inspector_access_token="APP_TOKEN_123",
        transport=transport,
    )

    # Target with leading/trailing whitespace and act_ prefix
    res = adapter.inspect_token(target_ad_account_id="  act_1985595022114520 \t\n")
    assert res.status == TokenStatus.VALID
    assert res.is_valid is True

    # Malformed target account ID is rejected immediately
    res_bad = adapter.inspect_token(target_ad_account_id="act_act_1985595022114520")
    assert res_bad.status == TokenStatus.INVALID
    assert res_bad.is_valid is False
    assert "malformed or invalid" in (res_bad.rejection_reason or "")


# ==============================================================================
# 3. Strict normalize_ad_account_id and MetaExecutionSpecification Validation
# ==============================================================================

def test_normalize_ad_account_id_strict_validation():
    """Verify MetaMarketingApiAdapter.normalize_ad_account_id strictly validates ASCII digits."""
    # Valid formats normalize to canonical act_<digits>
    assert MetaMarketingApiAdapter.normalize_ad_account_id("1985595022114520") == "act_1985595022114520"
    assert MetaMarketingApiAdapter.normalize_ad_account_id("act_1985595022114520") == "act_1985595022114520"
    assert MetaMarketingApiAdapter.normalize_ad_account_id("  1985595022114520  ") == "act_1985595022114520"
    assert MetaMarketingApiAdapter.normalize_ad_account_id("  act_1985595022114520  ") == "act_1985595022114520"

    # Malformed strings raise ValueError
    invalid_inputs = [
        "act_act_1985595022114520",
        "act_19855abc",
        "abcdef",
        "19855.95022114520",
        "19855-95022114520",
        "act_ 1985595022114520",
        "\u0661\u0662\u0663\u0664\u0665",
    ]
    for bad_id in invalid_inputs:
        with pytest.raises(ValueError, match="Invalid Meta ad account ID"):
            MetaMarketingApiAdapter.normalize_ad_account_id(bad_id)


def test_meta_execution_specification_strict_account_validation(valid_spec: MetaExecutionSpecification):
    """Verify MetaExecutionSpecification enforces strict account ID validation at contract boundary."""
    # Valid spec with un-prefixed account ID normalizes to act_
    spec = MetaExecutionSpecification(
        experiment_id=valid_spec.experiment_id,
        ad_account_id="1985595022114520",
        page_id=valid_spec.page_id,
        destination_url=valid_spec.destination_url,
        primary_text=valid_spec.primary_text,
        headline=valid_spec.headline,
        image_hash=valid_spec.image_hash,
        authorized_budget=valid_spec.authorized_budget,
    )
    assert spec.ad_account_id == "act_1985595022114520"

    # Malformed repeated prefix is rejected
    with pytest.raises(ValueError, match="Invalid Meta ad account ID"):
        MetaExecutionSpecification(
            experiment_id=valid_spec.experiment_id,
            ad_account_id="act_act_1985595022114520",
            page_id=valid_spec.page_id,
            destination_url=valid_spec.destination_url,
            primary_text=valid_spec.primary_text,
            headline=valid_spec.headline,
            image_hash=valid_spec.image_hash,
            authorized_budget=valid_spec.authorized_budget,
        )


# ==============================================================================
# 4. Legacy Emergency Stop Enforces Caller safe_mode=True
# ==============================================================================

def test_legacy_emergency_stop_enforces_caller_safe_mode_true(
    session: Session,
    approved_experiment: ExperimentORM,
    existing_execution: ExternalExecutionORM,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify that _emergency_stop blocks external mutations if safe_mode=True is passed, even when VENTUREBOT_SAFE_MODE=false."""
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "false")
    transport = RecordingFakeTransport()
    adapter = MetaMarketingApiAdapter(
        ad_account_id=SAMPLE_ACCOUNT_ID,
        access_token=SAMPLE_TOKEN,
        transport=transport,
    )

    # Caller explicitly requests safe_mode=True
    res = MetaExperimentDispatchService._emergency_stop(
        session=session,
        experiment_id=approved_experiment.id,
        reason="Testing safe_mode=True enforcement",
        adapter=adapter,
        safe_mode=True,
    )
    assert res.is_successful is False
    assert "SAFE_MODE_ENABLED" in (res.rejection_reason or "")
    assert transport.write_count == 0


# ==============================================================================
# 5. Secret Sanitizer Redacts URL Parameters, Bearer Tokens, and JSON Payloads
# ==============================================================================

def test_sanitize_error_redacts_across_formats():
    """Verify sanitize_error redacts tokens in URL parameters, Bearer headers, and JSON strings."""
    url_err = "Request to https://graph.facebook.com/v20.0/debug_token?input_token=EAAB12345&access_token=APP123 failed"
    sanitized_url = sanitize_error(url_err)
    assert "EAAB12345" not in sanitized_url
    assert "APP123" not in sanitized_url
    assert "input_token=[REDACTED]" in sanitized_url
    assert "access_token=[REDACTED]" in sanitized_url

    bearer_err = "Failed with Authorization: Bearer EAAB99999XYZ and appsecret_proof=PROOF123"
    sanitized_bearer = sanitize_error(bearer_err)
    assert "EAAB99999XYZ" not in sanitized_bearer
    assert "PROOF123" not in sanitized_bearer
    assert "Bearer [REDACTED]" in sanitized_bearer
    assert "appsecret_proof=[REDACTED]" in sanitized_bearer

    json_err = 'Meta returned {"error": {"message": "Invalid token", "access_token": "SECRET123", "input_token": "TOKEN456"}}'
    sanitized_json = sanitize_error(json_err)
    assert "SECRET123" not in sanitized_json
    assert "TOKEN456" not in sanitized_json
    assert '"access_token": "[REDACTED]"' in sanitized_json
    assert '"input_token": "[REDACTED]"' in sanitized_json


# ==============================================================================
# 6. classify_execution_lineage Detects Versioned Events with Numeric adset_id
# ==============================================================================

def test_classify_execution_lineage_detects_versioned_event_with_numeric_adset_id(
    approved_experiment: ExperimentORM,
):
    """Verify classify_execution_lineage detects recovery execution when event logs versioned name."""
    exec_rec = ExternalExecution(
        experiment_id=approved_experiment.id,
        channel="meta",
        external_account_id="1985595022114520",
        campaign_id="120252176243860380",
        adset_id="120252176299999999",  # purely numeric Meta ID!
        status=ExternalExecutionStatus.DEPLOYED_DORMANT,
    )
    events = [
        ExternalExecutionEvent(
            execution_id=exec_rec.id,
            event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            tier="adset",
            old_resource_id="120252176254130380",
            new_resource_id=None,
            reason="Original ad set superseded by VB-EXP-test-ADSET-v2 for fresh 48h flight",
        ),
        ExternalExecutionEvent(
            execution_id=exec_rec.id,
            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
            tier="adset",
            old_resource_id=None,
            new_resource_id="120252176299999999",
            reason="Created dormant replacement ad set with 24h buffer schedule",
            api_request_summary=json.dumps({"name": "VB-EXP-test-ADSET-v2", "version": 2}),
        ),
    ]

    classification, orig_id, anomaly = classify_execution_lineage(exec_rec, events)
    assert classification == RecoveryLineageClassification.OPTION_B
    assert orig_id == "120252176254130380"
    assert anomaly is None
