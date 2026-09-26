"""Comprehensive tests for ExecutionDispatchService and safety guardrails (Step 40)."""

from __future__ import annotations

from decimal import Decimal
from typing import Generator
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.models import ExperimentORM, OpportunityORM
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.env import is_safe_mode
from venturebot.execution.dispatch import (
    ExecutionAction,
    ExecutionDispatchResult,
    ExecutionDispatchService,
    ExecutionRequest,
    MetaExperimentDispatchService,
    _GatewayInvocationContext,
)
from venturebot.execution.meta import (
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
)
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.external_execution import ExternalExecutionStatus
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Create a fresh in-memory SQLite database session for testing."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def approved_experiment(db_session: Session) -> ExperimentORM:
    """Seed and return a valid approved ExperimentORM instance with allocated capital."""
    opp_repo = OpportunityRepository(db_session, auto_commit=True)
    exp_repo = ExperimentRepository(db_session, auto_commit=True)

    opp = Opportunity(
        title="B2B Lead Generation Pilot",
        description="A test opportunity for lead generation",
        audience="Small Business Owners",
        category=OpportunityCategory.SERVICE,
        status=OpportunityStatus.APPROVED,
        confidence=0.85,
        estimated_revenue_min=Decimal("10000.00"),
        estimated_revenue_max=Decimal("20000.00"),
        estimated_cost_min=Decimal("200.00"),
        estimated_cost_max=Decimal("500.00"),
    )

    opp_orm = opp_repo.create(opp)

    exp = Experiment(
        opportunity_id=opp_orm.id,
        hypothesis="Targeted traffic yields qualified leads at positive ROI",
        objective="Generate 20 qualified leads",
        channel=Channel.FACEBOOK,
        monetization_method=MonetizationMethod.LEAD_GEN,
        allocated_budget=Decimal("500.00"),
        max_allowed_spend=Decimal("500.00"),
        actual_spend=Decimal("0.00"),
        success_criteria="CAC <= 25 INR",
        failure_criteria="CAC > 50 INR",
        status=ExperimentStatus.APPROVED,
    )
    exp_repo.create(exp)
    exp_orm = db_session.get(ExperimentORM, exp.id)
    assert exp_orm is not None
    return exp_orm



# ── ExecutionRequest Model Validations ─────────────────────────────────────────


def test_request_model_valid():
    """Valid ExecutionRequest initializes properly with permitted fields."""
    exp_id = uuid4()
    req = ExecutionRequest(
        experiment_id=exp_id,
        requested_action=ExecutionAction.CREATE_CAMPAIGN,
        proposed_budget=Decimal("150.00"),
        metadata={"note": "Test run"},
    )
    assert req.experiment_id == exp_id
    assert req.requested_action == "CREATE_CAMPAIGN"
    assert req.proposed_budget == Decimal("150.00")
    assert req.metadata == {"note": "Test run"}


def test_request_model_accepts_valid_string_action():
    """String action is normalized and accepted if valid."""
    req = ExecutionRequest(
        experiment_id=uuid4(),
        requested_action="create_adset",
        proposed_budget=Decimal("100.00"),
    )
    assert req.requested_action == "CREATE_ADSET"


def test_request_model_rejects_invalid_action():
    """Unsupported action strings are rejected by validation."""
    with pytest.raises(ValidationError, match="requested_action must be one of"):
        ExecutionRequest(
            experiment_id=uuid4(),
            requested_action="DELETE_CAMPAIGN",
            proposed_budget=Decimal("100.00"),
        )


def test_request_model_rejects_zero_or_negative_budget():
    """Proposed budget must be strictly positive."""
    with pytest.raises(ValidationError, match="proposed_budget must be strictly positive"):
        ExecutionRequest(
            experiment_id=uuid4(),
            requested_action=ExecutionAction.CREATE_CAMPAIGN,
            proposed_budget=Decimal("0.00"),
        )

    with pytest.raises(ValidationError, match="proposed_budget must be strictly positive"):
        ExecutionRequest(
            experiment_id=uuid4(),
            requested_action=ExecutionAction.CREATE_CAMPAIGN,
            proposed_budget=Decimal("-50.00"),
        )


# ── Guardrail: Safe Mode Default & Blocking ────────────────────────────────────


def test_block_when_safe_mode_true(approved_experiment: ExperimentORM, db_session: Session):
    """Execution is BLOCKED when SAFE_MODE is enabled (default behavior)."""
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("100.00"),
    )

    result = ExecutionDispatchService.dispatch(req, session=db_session, safe_mode=True)

    assert result.blocked is True
    assert result.success is False
    assert result.reason == "SAFE_MODE_ENABLED"
    assert result.action_attempted == "DEPLOY_EXPERIMENT"
    assert result.experiment_id == approved_experiment.id


def test_valid_request_returns_blocked_true_safe_mode(
    approved_experiment: ExperimentORM, db_session: Session
):
    """A completely valid request returns blocked=True with reason 'SAFE_MODE_ENABLED'."""
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("250.00"),
    )

    # Calling with default safe_mode (reads env/default True)
    result = ExecutionDispatchService.dispatch(req, session=db_session)

    assert isinstance(result, ExecutionDispatchResult)
    assert result.blocked is True
    assert result.success is False
    assert result.reason == "SAFE_MODE_ENABLED"
    assert result.action_attempted == "DEPLOY_EXPERIMENT"
    assert result.experiment_id == approved_experiment.id


# ── Guardrail: Experiment Status Must Be APPROVED ──────────────────────────────


@pytest.mark.parametrize(
    "invalid_status",
    [
        ExperimentStatus.DRAFT,
        ExperimentStatus.RUNNING,
        ExperimentStatus.PAUSED,
        ExperimentStatus.COMPLETED,
        ExperimentStatus.KILLED,
    ],
)
def test_block_when_experiment_not_approved(
    invalid_status: ExperimentStatus,
    approved_experiment: ExperimentORM,
    db_session: Session,
):
    """Execution is BLOCKED if experiment is not in APPROVED status."""
    approved_experiment.status = invalid_status.value
    db_session.commit()

    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.CREATE_CAMPAIGN,
        proposed_budget=Decimal("100.00"),
    )

    result = ExecutionDispatchService.dispatch(req, session=db_session)

    assert result.blocked is True
    assert result.success is False
    assert f"'{invalid_status.value}'" in result.reason
    assert "approved" in result.reason



# ── Guardrail: Budget Limits & Allocated Capital ───────────────────────────────


def test_block_when_budget_exceeds_allocation(
    approved_experiment: ExperimentORM, db_session: Session
):
    """Execution is BLOCKED if proposed_budget exceeds remaining budget."""
    # allocated_budget = 500, actual_spend = 400 -> remaining = 100
    approved_experiment.actual_spend = Decimal("400.00")
    db_session.commit()

    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.CREATE_CAMPAIGN,
        proposed_budget=Decimal("150.00"),  # 150 > 100 remaining
    )

    result = ExecutionDispatchService.dispatch(req, session=db_session)

    assert result.blocked is True
    assert result.success is False
    assert "exceeds remaining budget" in result.reason


def test_block_when_experiment_has_no_allocated_capital(
    approved_experiment: ExperimentORM, db_session: Session
):
    """Execution is BLOCKED if experiment has 0 allocated budget."""
    approved_experiment.allocated_budget = Decimal("0.00")
    db_session.commit()

    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.CREATE_CAMPAIGN,
        proposed_budget=Decimal("50.00"),
    )

    result = ExecutionDispatchService.dispatch(req, session=db_session)

    assert result.blocked is True
    assert result.success is False
    assert "has no allocated capital" in result.reason


# ── Guardrail: Experiment Not Found ───────────────────────────────────────────


def test_block_when_experiment_not_found(db_session: Session):
    """Execution is BLOCKED if the experiment does not exist."""
    random_id = uuid4()
    req = ExecutionRequest(
        experiment_id=random_id,
        requested_action=ExecutionAction.CREATE_CAMPAIGN,
        proposed_budget=Decimal("100.00"),
    )

    result = ExecutionDispatchService.dispatch(req, session=db_session)

    assert result.blocked is True
    assert result.success is False
    assert f"Experiment '{random_id}' not found" in result.reason
    assert result.experiment_id == random_id


# ── Guardrail: Future Meta Identifiers ────────────────────────────────────────


def test_block_when_missing_required_meta_identifiers(
    approved_experiment: ExperimentORM, db_session: Session
):
    """Execution is BLOCKED if metadata requests Meta identifiers that are missing."""
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("100.00"),
        metadata={"require_meta_identifiers": True},
    )

    result = ExecutionDispatchService.dispatch(req, session=db_session)

    assert result.blocked is True
    assert result.success is False
    assert "Missing required Meta identifier" in result.reason


def test_pass_identifier_check_when_identifiers_provided(
    approved_experiment: ExperimentORM, db_session: Session
):
    """Identifier guardrail passes when required Meta identifier is present, falling through to SAFE_MODE."""
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("100.00"),
        metadata={"require_meta_identifiers": True, "ad_account_id": "act_123456789"},
    )

    result = ExecutionDispatchService.dispatch(req, session=db_session)

    assert result.blocked is True
    assert result.reason == "SAFE_MODE_ENABLED"


# ── Safe Mode Environment Variable Toggle ──────────────────────────────────────


def test_safe_mode_env_var(monkeypatch: pytest.MonkeyPatch):
    """VENTUREBOT_SAFE_MODE correctly controls is_safe_mode()."""
    # Unset defaults to True
    monkeypatch.delenv("VENTUREBOT_SAFE_MODE", raising=False)
    assert is_safe_mode() is True

    # Explicit "true"
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "true")
    assert is_safe_mode() is True

    # Explicit "1"
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "1")
    assert is_safe_mode() is True

    # Explicit "false"
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "false")
    assert is_safe_mode() is False

    # Explicit "0"
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "0")
    assert is_safe_mode() is False

    # Explicit "no"
    monkeypatch.setenv("VENTUREBOT_SAFE_MODE", "no")
    assert is_safe_mode() is False


# ── Guardrail: Passing Domain Model Directly ───────────────────────────────────


def test_dispatch_with_domain_model_directly():
    """ExecutionDispatchService can accept an in-memory Experiment domain model directly."""
    exp = Experiment(
        id=uuid4(),
        opportunity_id=uuid4(),
        hypothesis="In-memory test hypothesis",
        objective="Test in-memory domain validation",
        channel=Channel.INSTAGRAM,
        monetization_method=MonetizationMethod.DIRECT_SALE,
        allocated_budget=Decimal("200.00"),
        max_allowed_spend=Decimal("200.00"),
        actual_spend=Decimal("50.00"),
        success_criteria="Works",
        failure_criteria="Fails",
        status=ExperimentStatus.APPROVED,
    )

    assert exp.remaining_budget == Decimal("150.00")

    # Valid budget under remaining
    req = ExecutionRequest(
        experiment_id=exp.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("100.00"),
    )
    res = ExecutionDispatchService.dispatch(req, experiment=exp)
    assert res.blocked is True
    assert res.reason == "SAFE_MODE_ENABLED"

    # Exceeding remaining budget
    req_excess = ExecutionRequest(
        experiment_id=exp.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("160.00"),
    )
    res_excess = ExecutionDispatchService.dispatch(req_excess, experiment=exp)
    assert res_excess.blocked is True
    assert "exceeds remaining budget" in res_excess.reason


# ── Strict Verification: Zero Side Effects ────────────────────────────────────


def test_zero_side_effects(approved_experiment: ExperimentORM, db_session: Session):
    """Verify strictly 0 DB mutations, 0 capital transactions, and 0 external side effects."""
    cap_repo = CapitalRepository(db_session, auto_commit=True)
    initial_tx_count = len(cap_repo.get_transaction_history())

    # Snapshot experiment state
    exp_id = approved_experiment.id
    initial_status = approved_experiment.status
    initial_spend = approved_experiment.actual_spend
    initial_allocated = approved_experiment.allocated_budget

    # Submit multiple dispatch requests (both blocked by validation and blocked by safe mode)
    req_valid = ExecutionRequest(
        experiment_id=exp_id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("100.00"),
    )
    req_invalid_budget = ExecutionRequest(
        experiment_id=exp_id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("9999.00"),
    )

    res1 = ExecutionDispatchService.dispatch(req_valid, session=db_session)
    res2 = ExecutionDispatchService.dispatch(req_invalid_budget, session=db_session)

    assert res1.blocked is True
    assert res2.blocked is True

    # 1. Zero capital transactions
    assert len(cap_repo.get_transaction_history()) == initial_tx_count
    assert cap_repo.get_experiment_actual_spend(exp_id) == Decimal("0.00")


    # 2. Zero experiment mutations
    db_session.expire_all()
    exp_after = db_session.get(ExperimentORM, exp_id)
    assert exp_after is not None
    assert exp_after.status == initial_status
    assert exp_after.actual_spend == initial_spend
    assert exp_after.allocated_budget == initial_allocated


# ── Step 41 — Consolidated Gateway Tests (A through J) ────────────────────────


def _make_mock_meta_transport():
    """Helper to produce a mock transport for MetaMarketingApiAdapter."""
    import json
    import urllib.request

    def _transport(req: urllib.request.Request, timeout: float = 10.0) -> bytes:
        url = req.full_url
        method = req.get_method()

        # Account metadata
        if "/act_1985595022114520?fields=" in url:
            return json.dumps({
                "id": "act_1985595022114520",
                "name": "VentureBot Experiments",
                "account_status": 1,
                "currency": "INR",
            }).encode("utf-8")

        # Duplicate check
        if "/campaigns?fields=" in url and method == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Ad set lookup
        if "/adsets?fields=" in url and method == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Creative lookup
        if "/adcreatives?fields=" in url and method == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Ad lookup
        if "/ads?fields=" in url and method == "GET":
            return json.dumps({"data": []}).encode("utf-8")

        # Campaign creation
        if "/campaigns" in url and method == "POST":
            return json.dumps({"id": "camp_step41"}).encode("utf-8")

        # Ad set creation
        if "/adsets" in url and method == "POST":
            return json.dumps({"id": "adset_step41"}).encode("utf-8")

        # Creative creation
        if "/adcreatives" in url and method == "POST":
            return json.dumps({"id": "creative_step41"}).encode("utf-8")

        # Ad creation
        if "/ads" in url and method == "POST":
            return json.dumps({"id": "ad_step41"}).encode("utf-8")

        # Campaign pause
        if method == "POST" and "camp_" in url:
            return json.dumps({"success": True}).encode("utf-8")

        return json.dumps({"id": "default_mock_id"}).encode("utf-8")

    return _transport


def _make_valid_meta_spec(experiment_id) -> MetaExecutionSpecification:
    from datetime import datetime, timezone
    return MetaExecutionSpecification(
        experiment_id=experiment_id,
        ad_account_id="act_1985595022114520",
        page_id="10987654321",
        destination_url="https://example.com/playbook",
        primary_text="Test primary text copy for ad.",
        headline="Test Headline",
        authorized_budget=Decimal("50.00"),
        end_time=datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc),
        image_hash="img_hash_step41",
        explicit_dispatch_authorized=True,
    )


def test_step41_a_deploy_experiment_action_exists():
    """A. Verify DEPLOY_EXPERIMENT exists in ExecutionAction and is accepted."""
    assert ExecutionAction.DEPLOY_EXPERIMENT == "DEPLOY_EXPERIMENT"
    assert "DEPLOY_EXPERIMENT" in {a.value for a in ExecutionAction}

    exp_id = uuid4()
    req = ExecutionRequest(
        experiment_id=exp_id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("50.00"),
    )
    assert req.requested_action == "DEPLOY_EXPERIMENT"


def test_step41_b_create_campaign_rejected_by_public_gateway(
    approved_experiment: ExperimentORM, db_session: Session
):
    """B. Verify CREATE_CAMPAIGN is rejected by the public gateway with locked reason."""
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.CREATE_CAMPAIGN,
        proposed_budget=Decimal("50.00"),
    )
    res = ExecutionDispatchService.dispatch(req, session=db_session)
    assert res.blocked is True
    assert res.success is False
    assert res.reason == "GRANULAR_ACTIONS_NOT_PERMITTED_IN_PUBLIC_GATEWAY"
    assert res.action_attempted == "CREATE_CAMPAIGN"


def test_step41_c_create_adset_rejected_by_public_gateway(
    approved_experiment: ExperimentORM, db_session: Session
):
    """C. Verify CREATE_ADSET is rejected by the public gateway with locked reason."""
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.CREATE_ADSET,
        proposed_budget=Decimal("50.00"),
    )
    res = ExecutionDispatchService.dispatch(req, session=db_session)
    assert res.blocked is True
    assert res.success is False
    assert res.reason == "GRANULAR_ACTIONS_NOT_PERMITTED_IN_PUBLIC_GATEWAY"
    assert res.action_attempted == "CREATE_ADSET"


def test_step41_d_create_ad_rejected_by_public_gateway(
    approved_experiment: ExperimentORM, db_session: Session
):
    """D. Verify CREATE_AD is rejected by the public gateway with locked reason."""
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.CREATE_AD,
        proposed_budget=Decimal("50.00"),
    )
    res = ExecutionDispatchService.dispatch(req, session=db_session)
    assert res.blocked is True
    assert res.success is False
    assert res.reason == "GRANULAR_ACTIONS_NOT_PERMITTED_IN_PUBLIC_GATEWAY"
    assert res.action_attempted == "CREATE_AD"


def test_step41_e_safe_mode_true_blocks_deploy_experiment_before_tier2(
    approved_experiment: ExperimentORM, db_session: Session
):
    """E. Verify SAFE_MODE=True blocks DEPLOY_EXPERIMENT before Tier 2 is invoked."""
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("50.00"),
    )
    res = ExecutionDispatchService.dispatch(req, session=db_session, safe_mode=True)
    assert res.blocked is True
    assert res.success is False
    assert res.reason == "SAFE_MODE_ENABLED"
    assert res.action_attempted == "DEPLOY_EXPERIMENT"


def test_step41_f_safe_mode_false_allows_request_to_reach_tier2_using_mock_transport(
    approved_experiment: ExperimentORM, db_session: Session
):
    """F. Verify SAFE_MODE=False allows the request to reach Tier 2 using mocked transport."""
    spec = _make_valid_meta_spec(approved_experiment.id)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=spec.ad_account_id,
        access_token="fake_token_test",
        transport=_make_mock_meta_transport(),
    )

    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("50.00"),
        metadata={"channel": "meta"},
    )

    res = ExecutionDispatchService.dispatch(
        req,
        session=db_session,
        safe_mode=False,
        spec=spec,
        adapter=adapter,
    )

    assert res.success is True
    assert res.blocked is False
    assert res.reason == "DEPLOYED"
    assert res.execution is not None
    assert res.execution.status == ExternalExecutionStatus.DEPLOYED
    assert res.execution.campaign_id == "camp_step41"
    assert res.execution.adset_id == "adset_step41"
    assert res.execution.creative_id == "creative_step41"
    assert res.execution.ad_id == "ad_step41"
    assert res.experiment is not None
    assert res.experiment.status == ExperimentStatus.RUNNING


def test_step41_g_meta_dispatcher_has_no_public_dispatch_method():
    """G. Verify MetaExperimentDispatchService has NO public dispatch() execution method."""
    assert not hasattr(MetaExperimentDispatchService, "dispatch")
    assert hasattr(MetaExperimentDispatchService, "_dispatch_from_gateway")


def test_step41_h_dispatch_from_gateway_rejects_missing_or_invalid_context(
    approved_experiment: ExperimentORM, db_session: Session
):
    """H. Verify _dispatch_from_gateway() rejects missing or invalid gateway context."""
    spec = _make_valid_meta_spec(approved_experiment.id)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=spec.ad_account_id,
        access_token="fake_token_test",
        transport=_make_mock_meta_transport(),
    )

    # 1. Non-context object
    res_none = MetaExperimentDispatchService._dispatch_from_gateway(
        session=db_session,
        experiment_id=approved_experiment.id,
        spec=spec,
        adapter=adapter,
        safe_mode=False,
        gateway_context=None,  # type: ignore[arg-type]
    )
    assert res_none.is_successful is False
    assert "Missing or invalid gateway invocation context" in (res_none.rejection_reason or "")

    # 2. String/boolean pretending to be trust flag
    res_fake = MetaExperimentDispatchService._dispatch_from_gateway(
        session=db_session,
        experiment_id=approved_experiment.id,
        spec=spec,
        adapter=adapter,
        safe_mode=False,
        gateway_context=True,  # type: ignore[arg-type]
    )
    assert res_fake.is_successful is False
    assert "Missing or invalid gateway invocation context" in (res_fake.rejection_reason or "")

    # 3. Context with mismatched experiment_id
    mismatched_ctx = _GatewayInvocationContext(
        experiment_id=uuid4(),
        proposed_budget=Decimal("50.00"),
        channel="meta",
    )
    res_mismatch = MetaExperimentDispatchService._dispatch_from_gateway(
        session=db_session,
        experiment_id=approved_experiment.id,
        spec=spec,
        adapter=adapter,
        safe_mode=False,
        gateway_context=mismatched_ctx,
    )
    assert res_mismatch.is_successful is False
    assert "does not match target experiment" in (res_mismatch.rejection_reason or "")


def test_step41_i_dispatch_from_gateway_independently_blocks_when_safe_mode_true(
    approved_experiment: ExperimentORM, db_session: Session
):
    """I. Verify _dispatch_from_gateway() independently blocks when SAFE_MODE=True."""
    spec = _make_valid_meta_spec(approved_experiment.id)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=spec.ad_account_id,
        access_token="fake_token_test",
        transport=_make_mock_meta_transport(),
    )
    ctx = _GatewayInvocationContext(
        experiment_id=approved_experiment.id,
        proposed_budget=Decimal("50.00"),
        channel="meta",
    )

    # Calling with safe_mode=True
    res = MetaExperimentDispatchService._dispatch_from_gateway(
        session=db_session,
        experiment_id=approved_experiment.id,
        spec=spec,
        adapter=adapter,
        safe_mode=True,
        gateway_context=ctx,
    )
    assert res.is_successful is False
    assert res.rejection_reason == "SAFE_MODE_ENABLED"


def test_step41_j_emergency_stop_gateway(
    approved_experiment: ExperimentORM, db_session: Session
):
    """J. Verify ExecutionDispatchService.emergency_stop() coordinates external pause then internal kill."""
    spec = _make_valid_meta_spec(approved_experiment.id)
    adapter = MetaMarketingApiAdapter(
        ad_account_id=spec.ad_account_id,
        access_token="fake_token_test",
        transport=_make_mock_meta_transport(),
    )

    # Deploy first via gateway
    req = ExecutionRequest(
        experiment_id=approved_experiment.id,
        requested_action=ExecutionAction.DEPLOY_EXPERIMENT,
        proposed_budget=Decimal("50.00"),
    )
    disp_res = ExecutionDispatchService.dispatch(
        req,
        session=db_session,
        safe_mode=False,
        spec=spec,
        adapter=adapter,
    )
    assert disp_res.success is True

    # Now trigger emergency stop via public ExecutionDispatchService gateway
    stop_res = ExecutionDispatchService.emergency_stop(
        session=db_session,
        experiment_id=approved_experiment.id,
        reason="Operator triggered public emergency stop.",
        adapter=adapter,
    )
    assert stop_res.is_successful is True
    assert stop_res.experiment is not None
    assert stop_res.experiment.status == ExperimentStatus.KILLED

