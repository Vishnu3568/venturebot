"""Meta experiment dispatch orchestration service for VentureBot (Step 39)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentORM, OpportunityORM
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.env import is_safe_mode
from venturebot.execution.meta import (
    MetaApiError,
    MetaApiTimeoutError,
    MetaExecutionSpecification,
    MetaMarketingApiAdapter,
    ReconciliationStatus,
    deterministic_ad_name,
    deterministic_adset_name,
    deterministic_campaign_name,
    deterministic_creative_name,
    inr_to_paise,
)
from venturebot.execution.service import ExperimentExecutionService
from venturebot.models.experiment import Experiment, ExperimentStatus
from venturebot.models.external_execution import (
    ExternalExecution,
    ExternalExecutionEvent,
    ExternalExecutionEventType,
    ExternalExecutionStatus,
)


class ExecutionAction(str, Enum):
    """Allowed actions for execution dispatch gateway requests."""

    DEPLOY_EXPERIMENT = "DEPLOY_EXPERIMENT"
    CREATE_CAMPAIGN = "CREATE_CAMPAIGN"
    CREATE_ADSET = "CREATE_ADSET"
    CREATE_AD = "CREATE_AD"


class ExecutionRequest(BaseModel):
    """Request envelope submitted to the safe execution dispatch gateway."""

    experiment_id: UUID
    requested_action: str
    proposed_budget: Decimal
    metadata: dict[str, Any] | None = None

    @field_validator("proposed_budget")
    @classmethod
    def validate_positive_budget(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0.00"):
            raise ValueError(f"proposed_budget must be strictly positive (> 0), got {v}")
        return v

    @field_validator("requested_action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        clean = v.strip().upper()
        allowed = {a.value for a in ExecutionAction}
        if clean not in allowed:
            raise ValueError(f"requested_action must be one of {allowed}, got '{v}'")
        return clean


class ExecutionDispatchResult(BaseModel):
    """Result returned by the safe execution dispatch layer."""

    success: bool
    blocked: bool
    reason: str
    action_attempted: str
    experiment_id: UUID
    execution: ExternalExecution | None = None
    experiment: Experiment | None = None


# Compatibility alias for callers importing ExecutionResult from dispatch.py
ExecutionResult = ExecutionDispatchResult


class _GatewayInvocationContext:
    """Private internal context token proving execution originated through ExecutionDispatchService.

    Constructed exclusively inside ExecutionDispatchService after Tier 1 guardrails pass.
    """

    def __init__(
        self,
        experiment_id: UUID,
        proposed_budget: Decimal,
        channel: str = "meta",
    ) -> None:
        self.experiment_id = experiment_id
        self.proposed_budget = proposed_budget
        self.channel = channel
        self.created_at = datetime.now(timezone.utc)


class ExecutionDispatchService:
    """Safe execution dispatch layer acting as the single gateway for future real-world actions.

    Validates safety rules and enforces strict invariants BEFORE any external call.
    In SAFE_MODE (default: True), unconditionally blocks execution with:
        {success: False, blocked: True, reason: "SAFE_MODE_ENABLED"}
    """

    @classmethod
    def dispatch(
        cls,
        request: ExecutionRequest,
        session: Session | None = None,
        experiment: Experiment | ExperimentORM | None = None,
        safe_mode: bool | None = None,
        spec: MetaExecutionSpecification | None = None,
        adapter: MetaMarketingApiAdapter | None = None,
    ) -> ExecutionDispatchResult:
        """Validate safety guardrails and dispatch execution request.

        Guardrail evaluation sequence:
        1. Experiment existence: Experiment must exist.
        2. Experiment status: Experiment must be in APPROVED status.
        3. Allocated capital: Experiment must have allocated_budget > 0.
        4. Budget limit: proposed_budget must not exceed remaining_budget.
        5. Granular action rejection: Granular actions (CREATE_CAMPAIGN, CREATE_ADSET,
           CREATE_AD) are not permitted through the public gateway.
        6. Future identifier check: Check optional future Meta identifiers.
        7. Global SAFE_MODE: If SAFE_MODE is True (default), block execution.
        8. Composite DEPLOY_EXPERIMENT delegation: Construct _GatewayInvocationContext
           and delegate to channel coordinator (MetaExperimentDispatchService._dispatch_from_gateway).
        """
        # 1. Resolve experiment
        target_exp = experiment
        if target_exp is None and session is not None:
            target_exp = session.get(ExperimentORM, request.experiment_id)

        if target_exp is None:
            return ExecutionDispatchResult(
                success=False,
                blocked=True,
                reason=f"Experiment '{request.experiment_id}' not found.",
                action_attempted=request.requested_action,
                experiment_id=request.experiment_id,
            )

        # 2. Status check: experiment must be APPROVED
        raw_status = (
            target_exp.status.value
            if isinstance(target_exp.status, ExperimentStatus)
            else str(target_exp.status).lower()
        )
        if raw_status != ExperimentStatus.APPROVED.value:
            return ExecutionDispatchResult(
                success=False,
                blocked=True,
                reason=(
                    f"Experiment '{request.experiment_id}' is in '{raw_status}' status. "
                    f"Only experiments in '{ExperimentStatus.APPROVED.value}' status can be dispatched."
                ),
                action_attempted=request.requested_action,
                experiment_id=request.experiment_id,
            )

        # 3. Capital check: experiment has allocated capital
        allocated = getattr(target_exp, "allocated_budget", Decimal("0.00"))
        if allocated <= Decimal("0.00"):
            return ExecutionDispatchResult(
                success=False,
                blocked=True,
                reason=f"Experiment '{request.experiment_id}' has no allocated capital.",
                action_attempted=request.requested_action,
                experiment_id=request.experiment_id,
            )

        # 4. Budget limit check: proposed_budget <= remaining_budget
        remaining = getattr(target_exp, "remaining_budget", None)
        if remaining is None:
            spend = getattr(target_exp, "actual_spend", Decimal("0.00"))
            remaining = max(Decimal("0.00"), allocated - spend)

        if request.proposed_budget > remaining:
            return ExecutionDispatchResult(
                success=False,
                blocked=True,
                reason=(
                    f"Proposed budget ({request.proposed_budget}) exceeds "
                    f"remaining budget ({remaining})."
                ),
                action_attempted=request.requested_action,
                experiment_id=request.experiment_id,
            )

        # 5. Granular actions rejection: Not permitted in public gateway
        if request.requested_action in (
            ExecutionAction.CREATE_CAMPAIGN.value,
            ExecutionAction.CREATE_ADSET.value,
            ExecutionAction.CREATE_AD.value,
            ExecutionAction.CREATE_CAMPAIGN,
            ExecutionAction.CREATE_ADSET,
            ExecutionAction.CREATE_AD,
        ):
            return ExecutionDispatchResult(
                success=False,
                blocked=True,
                reason="GRANULAR_ACTIONS_NOT_PERMITTED_IN_PUBLIC_GATEWAY",
                action_attempted=request.requested_action,
                experiment_id=request.experiment_id,
            )

        # 6. Future identifier check if requested in metadata
        if request.metadata and request.metadata.get("require_meta_identifiers"):
            meta_id = request.metadata.get("meta_account_id") or request.metadata.get("ad_account_id")
            if not meta_id:
                return ExecutionDispatchResult(
                    success=False,
                    blocked=True,
                    reason="Missing required Meta identifier: ad_account_id.",
                    action_attempted=request.requested_action,
                    experiment_id=request.experiment_id,
                )

        # 7. Global SAFE_MODE check (default True)
        effective_safe_mode = is_safe_mode() if safe_mode is None else safe_mode
        if effective_safe_mode:
            return ExecutionDispatchResult(
                success=False,
                blocked=True,
                reason="SAFE_MODE_ENABLED",
                action_attempted=request.requested_action,
                experiment_id=request.experiment_id,
            )

        # 8. Composite DEPLOY_EXPERIMENT delegation
        if request.requested_action in (
            ExecutionAction.DEPLOY_EXPERIMENT.value,
            ExecutionAction.DEPLOY_EXPERIMENT,
        ):
            channel = (
                request.metadata.get("channel")
                if request.metadata and request.metadata.get("channel")
                else "meta"
            )
            if channel == "meta":
                target_spec = spec or (request.metadata.get("spec") if request.metadata else None)
                target_adapter = adapter or (request.metadata.get("adapter") if request.metadata else None)

                if target_spec is None:
                    return ExecutionDispatchResult(
                        success=False,
                        blocked=True,
                        reason="Missing MetaExecutionSpecification for Meta deployment.",
                        action_attempted=request.requested_action,
                        experiment_id=request.experiment_id,
                    )

                if session is None:
                    return ExecutionDispatchResult(
                        success=False,
                        blocked=True,
                        reason="Active database session is required for Meta deployment.",
                        action_attempted=request.requested_action,
                        experiment_id=request.experiment_id,
                    )

                # Construct internal gateway invocation context
                ctx = _GatewayInvocationContext(
                    experiment_id=request.experiment_id,
                    proposed_budget=request.proposed_budget,
                    channel=channel,
                )

                meta_res = MetaExperimentDispatchService._dispatch_from_gateway(
                    session=session,
                    experiment_id=request.experiment_id,
                    spec=target_spec,
                    adapter=target_adapter,
                    safe_mode=safe_mode,
                    gateway_context=ctx,
                )
                return ExecutionDispatchResult(
                    success=meta_res.is_successful,
                    blocked=not meta_res.is_successful,
                    reason=meta_res.rejection_reason or "DEPLOYED",
                    action_attempted=request.requested_action,
                    experiment_id=request.experiment_id,
                    execution=meta_res.execution,
                    experiment=meta_res.experiment,
                )

            return ExecutionDispatchResult(
                success=False,
                blocked=True,
                reason=f"Unsupported execution channel: '{channel}'.",
                action_attempted=request.requested_action,
                experiment_id=request.experiment_id,
            )

        return ExecutionDispatchResult(
            success=False,
            blocked=True,
            reason=f"Unsupported action: '{request.requested_action}'.",
            action_attempted=request.requested_action,
            experiment_id=request.experiment_id,
        )

    @classmethod
    def emergency_stop(
        cls,
        session: Session,
        experiment_id: UUID,
        reason: str,
        adapter: MetaMarketingApiAdapter | None = None,
        auto_commit: bool = True,
    ) -> EmergencyStopResult:
        """Public emergency-stop gateway coordinating pause on external channel then internal kill."""
        return MetaExperimentDispatchService._emergency_stop(
            session=session,
            experiment_id=experiment_id,
            reason=reason,
            adapter=adapter,
            auto_commit=auto_commit,
        )

    @classmethod
    def deploy_recovery_dormant(
        cls,
        session: Session,
        experiment_id: UUID,
        spec: MetaExecutionSpecification,
        adapter: MetaMarketingApiAdapter | None = None,
        safe_mode: bool | None = None,
        explicit_auth: bool = False,
        version: int = 2,
        auto_commit: bool = True,
    ) -> RecoveryDormantResult:
        """Phase 1: Deploy dormant replacement ad set, creative, and ad under existing campaign.

        Invariants:
        1. SAFE_MODE must be False (explicitly un-gated for authorized execution).
        2. Explicit operator authorization is required.
        3. Experiment must exist and be in APPROVED status.
        4. Actual spend on financial ledger must be exactly 0.00.
        5. Allocated capital must be exactly ₹200.00; proposed budget <= allocated.
        6. Campaign budget sharing (CBO) must be disabled.
        7. Original expired ad set is preserved as-is (never deleted, modified, or archived).
        8. Audit event is recorded for superseding the original ad set.
        9. Replacement ad set is created in PAUSED status with a 24h buffer schedule.
        10. Creative and Ad are created in PAUSED status.
        11. Experiment status remains APPROVED (actual_start remains None).
        """
        effective_safe_mode = is_safe_mode() if safe_mode is None else safe_mode
        if effective_safe_mode:
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason="SAFE_MODE_ENABLED",
                experiment_id=experiment_id,
            )

        if not explicit_auth:
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason="EXPLICIT_AUTHORIZATION_REQUIRED",
                experiment_id=experiment_id,
            )

        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason="EXPERIMENT_NOT_FOUND",
                experiment_id=experiment_id,
            )

        if exp_orm.status != ExperimentStatus.APPROVED.value:
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason=f"Experiment in '{exp_orm.status}' status; expected APPROVED",
                experiment_id=experiment_id,
            )

        cap_repo = CapitalRepository(session, auto_commit=False)
        actual_spend = cap_repo.get_experiment_actual_spend(experiment_id)
        if actual_spend > Decimal("0.00"):
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason="ACTUAL_SPEND_EXCEEDS_ZERO",
                experiment_id=experiment_id,
            )

        if exp_orm.allocated_budget != Decimal("200.00"):
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason=f"Allocated budget is {exp_orm.allocated_budget}, expected 200.00",
                experiment_id=experiment_id,
            )

        if spec.authorized_budget > exp_orm.allocated_budget:
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason="AUTHORIZED_BUDGET_EXCEEDS_ALLOCATION",
                experiment_id=experiment_id,
            )

        exec_repo = ExternalExecutionRepository(session, auto_commit=auto_commit)
        execution = exec_repo.get_by_experiment_id(experiment_id)
        if execution is None or not execution.campaign_id or not execution.image_hash:
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason="MISSING_EXISTING_CAMPAIGN_OR_IMAGE",
                experiment_id=experiment_id,
            )

        if adapter is None:
            adapter = MetaMarketingApiAdapter(ad_account_id=spec.ad_account_id)

        try:
            if adapter.get_campaign_budget_sharing(execution.campaign_id):
                return RecoveryDormantResult(
                    success=False,
                    blocked=True,
                    reason="CAMPAIGN_BUDGET_SHARING_NOT_ALLOWED",
                    experiment_id=experiment_id,
                )
        except Exception as e:
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason=f"Failed to verify campaign budget sharing: {_sanitize_error(str(e))}",
                experiment_id=experiment_id,
            )

        # Record superseding of the original ad set if present (do not archive or delete remote object)
        old_adset_id = execution.adset_id
        if old_adset_id:
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
                    tier="adset",
                    old_resource_id=old_adset_id,
                    new_resource_id=None,
                    reason=f"Original ad set {old_adset_id} superseded by versioned ADSET-v{version} for fresh 48h flight",
                )
            )

        # Replacement Ad Set
        replacement_name = deterministic_adset_name(experiment_id, version=version)
        new_adset_id: str | None = None
        adset_lookup = adapter.lookup_adset(
            campaign_id=execution.campaign_id,
            adset_name=replacement_name,
            expected_lifetime_budget_paise=inr_to_paise(spec.authorized_budget),
        )

        if adset_lookup.status == ReconciliationStatus.FOUND_EXACT:
            new_adset_id = adset_lookup.resource_id
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.RESOURCE_ADOPTED.value,
                    tier="adset",
                    old_resource_id=None,
                    new_resource_id=new_adset_id,
                    reason="Adopted existing replacement ad set",
                )
            )
        elif adset_lookup.status == ReconciliationStatus.NOT_FOUND:
            now_utc = datetime.now(timezone.utc)
            buffer_start = now_utc + timedelta(hours=24)
            buffer_end = buffer_start + timedelta(hours=48)
            try:
                new_adset_id = adapter.create_adset(
                    campaign_id=execution.campaign_id,
                    name=replacement_name,
                    lifetime_budget_paise=inr_to_paise(spec.authorized_budget),
                    end_time=buffer_end,
                    start_time=buffer_start,
                    countries=spec.countries,
                    age_min=spec.age_min,
                    age_max=spec.age_max,
                    status="PAUSED",
                    ad_account_id=spec.ad_account_id,
                )
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
                        tier="adset",
                        old_resource_id=None,
                        new_resource_id=new_adset_id,
                        reason="Created dormant replacement ad set with 24h buffer schedule",
                    )
                )
            except Exception as e:
                execution.status = ExternalExecutionStatus.FAILED
                execution.last_error = f"Error creating replacement ad set: {_sanitize_error(str(e))}"
                execution = exec_repo.save(execution)
                return RecoveryDormantResult(
                    success=False,
                    blocked=False,
                    reason=execution.last_error,
                    experiment_id=experiment_id,
                    execution=execution,
                )
        else:
            return RecoveryDormantResult(
                success=False,
                blocked=True,
                reason=f"Ambiguous or unknown replacement ad set state: {adset_lookup.error_message}",
                experiment_id=experiment_id,
            )

        execution.adset_id = new_adset_id
        execution.status = ExternalExecutionStatus.PARTIAL_ADSET
        execution.last_error = None
        execution = exec_repo.save(execution)

        # Creative Tier
        if not execution.creative_id:
            creative_name = deterministic_creative_name(experiment_id)
            c_lookup = adapter.lookup_creative(
                creative_name=creative_name,
                page_id=spec.page_id,
                image_hash=execution.image_hash or "",
                destination_url=spec.destination_url,
                ad_account_id=spec.ad_account_id,
            )
            if c_lookup.status == ReconciliationStatus.FOUND_EXACT:
                execution.creative_id = c_lookup.resource_id
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.RESOURCE_ADOPTED.value,
                        tier="creative",
                        old_resource_id=None,
                        new_resource_id=execution.creative_id,
                        reason="Adopted existing creative",
                    )
                )
            elif c_lookup.status == ReconciliationStatus.NOT_FOUND:
                try:
                    c_id = adapter.create_creative(
                        name=creative_name,
                        page_id=spec.page_id,
                        link=spec.destination_url,
                        message=spec.primary_text,
                        headline=spec.headline,
                        image_hash=execution.image_hash or "",
                        call_to_action=spec.call_to_action,
                        ad_account_id=spec.ad_account_id,
                    )
                    execution.creative_id = c_id
                    exec_repo.record_event(
                        ExternalExecutionEvent(
                            execution_id=execution.id,
                            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
                            tier="creative",
                            old_resource_id=None,
                            new_resource_id=c_id,
                            reason="Created ad creative in Live Mode",
                        )
                    )
                except Exception as e:
                    execution.status = ExternalExecutionStatus.FAILED
                    execution.last_error = f"Error creating creative: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return RecoveryDormantResult(
                        success=False,
                        blocked=False,
                        reason=execution.last_error,
                        experiment_id=experiment_id,
                        execution=execution,
                        adset_id=execution.adset_id,
                    )
            else:
                return RecoveryDormantResult(
                    success=False,
                    blocked=True,
                    reason=f"Ambiguous creative lookup: {c_lookup.error_message}",
                    experiment_id=experiment_id,
                    execution=execution,
                    adset_id=execution.adset_id,
                )

            execution.status = ExternalExecutionStatus.PARTIAL_CREATIVE
            execution.last_error = None
            execution = exec_repo.save(execution)

        # Ad Tier
        if not execution.ad_id:
            ad_name = deterministic_ad_name(experiment_id)
            ad_lookup = adapter.lookup_ad(
                adset_id=execution.adset_id,  # type: ignore[arg-type]
                ad_name=ad_name,
                creative_id=execution.creative_id,  # type: ignore[arg-type]
            )
            if ad_lookup.status == ReconciliationStatus.FOUND_EXACT:
                execution.ad_id = ad_lookup.resource_id
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.RESOURCE_ADOPTED.value,
                        tier="ad",
                        old_resource_id=None,
                        new_resource_id=execution.ad_id,
                        reason="Adopted existing ad",
                    )
                )
            elif ad_lookup.status == ReconciliationStatus.NOT_FOUND:
                try:
                    ad_id = adapter.create_ad(
                        name=ad_name,
                        adset_id=execution.adset_id,  # type: ignore[arg-type]
                        creative_id=execution.creative_id,  # type: ignore[arg-type]
                        status="PAUSED",
                        ad_account_id=spec.ad_account_id,
                    )
                    execution.ad_id = ad_id
                    exec_repo.record_event(
                        ExternalExecutionEvent(
                            execution_id=execution.id,
                            event_type=ExternalExecutionEventType.RESOURCE_CREATED.value,
                            tier="ad",
                            old_resource_id=None,
                            new_resource_id=ad_id,
                            reason="Created ad in PAUSED status for policy review",
                        )
                    )
                except Exception as e:
                    execution.status = ExternalExecutionStatus.FAILED
                    execution.last_error = f"Error creating ad: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return RecoveryDormantResult(
                        success=False,
                        blocked=False,
                        reason=execution.last_error,
                        experiment_id=experiment_id,
                        execution=execution,
                        adset_id=execution.adset_id,
                        creative_id=execution.creative_id,
                    )
            else:
                return RecoveryDormantResult(
                    success=False,
                    blocked=True,
                    reason=f"Ambiguous ad lookup: {ad_lookup.error_message}",
                    experiment_id=experiment_id,
                    execution=execution,
                    adset_id=execution.adset_id,
                    creative_id=execution.creative_id,
                )

            execution.status = ExternalExecutionStatus.DEPLOYED_DORMANT
            execution.last_error = None
            execution = exec_repo.save(execution)

        return RecoveryDormantResult(
            success=True,
            experiment_id=experiment_id,
            execution=execution,
            adset_id=execution.adset_id,
            creative_id=execution.creative_id,
            ad_id=execution.ad_id,
        )

    @classmethod
    def verify_readiness(
        cls,
        session: Session,
        experiment_id: UUID,
        adapter: MetaMarketingApiAdapter | None = None,
    ) -> ReadinessResult:
        """Phase 2a: Read-only verification of ad review status and readiness."""
        exec_repo = ExternalExecutionRepository(session)
        execution = exec_repo.get_by_experiment_id(experiment_id)
        blockers: list[str] = []

        if execution is None or not execution.ad_id or not execution.adset_id:
            return ReadinessResult(
                is_ready=False,
                ad_approved=False,
                ad_status="UNKNOWN",
                effective_status="UNKNOWN",
                schedule_is_future=False,
                cbo_disabled=False,
                blockers=["DEPLOYMENT_INCOMPLETE"],
            )

        if adapter is None:
            adapter = MetaMarketingApiAdapter(ad_account_id=execution.external_account_id)

        try:
            ad_info = adapter.get_ad_review_status(execution.ad_id)
        except Exception as e:
            return ReadinessResult(
                is_ready=False,
                ad_approved=False,
                ad_status="ERROR",
                effective_status="ERROR",
                schedule_is_future=False,
                cbo_disabled=False,
                blockers=[f"Failed to fetch ad review status: {_sanitize_error(str(e))}"],
            )

        try:
            adset_info = adapter.get_adset(execution.adset_id)
        except Exception as e:
            return ReadinessResult(
                is_ready=False,
                ad_approved=ad_info["is_approved"],
                ad_status=ad_info["status"],
                effective_status=ad_info["effective_status"],
                schedule_is_future=False,
                cbo_disabled=False,
                blockers=[f"Failed to fetch ad set schedule: {_sanitize_error(str(e))}"],
            )

        start_time_raw = adset_info.get("start_time")
        schedule_future = False
        if start_time_raw:
            try:
                # Parse ISO-8601 string
                clean_time = start_time_raw.replace("Z", "+00:00")
                st = datetime.fromisoformat(clean_time)
                schedule_future = st > datetime.now(timezone.utc)
            except Exception:
                schedule_future = False

        if not schedule_future:
            blockers.append("SCHEDULE_NOT_IN_FUTURE")
        if not ad_info["is_approved"]:
            blockers.append("AD_NOT_APPROVED")

        cbo_disabled = not adapter.get_campaign_budget_sharing(execution.campaign_id or "")
        if not cbo_disabled:
            blockers.append("CBO_ENABLED")

        is_ready = len(blockers) == 0
        exec_repo.record_event(
            ExternalExecutionEvent(
                execution_id=execution.id,
                event_type=ExternalExecutionEventType.READINESS_CHECKED.value,
                tier="orchestration",
                reason=f"Readiness check completed: is_ready={is_ready}, blockers={blockers}",
            )
        )

        return ReadinessResult(
            is_ready=is_ready,
            ad_approved=ad_info["is_approved"],
            ad_status=ad_info["status"],
            effective_status=ad_info["effective_status"],
            schedule_is_future=schedule_future,
            cbo_disabled=cbo_disabled,
            blockers=blockers,
        )

    @classmethod
    def lock_schedule(
        cls,
        session: Session,
        experiment_id: UUID,
        target_start: datetime,
        target_end: datetime,
        adapter: MetaMarketingApiAdapter | None = None,
        safe_mode: bool | None = None,
        explicit_auth: bool = False,
    ) -> ScheduleLockResult:
        """Phase 2b: Lock an exact 48.0-hour schedule prior to live activation."""
        effective_safe_mode = is_safe_mode() if safe_mode is None else safe_mode
        if effective_safe_mode:
            return ScheduleLockResult(success=False, blocked=True, reason="SAFE_MODE_ENABLED")

        if not explicit_auth:
            return ScheduleLockResult(success=False, blocked=True, reason="EXPLICIT_AUTHORIZATION_REQUIRED")

        now_utc = datetime.now(timezone.utc)
        if target_start.tzinfo is None or target_end.tzinfo is None:
            return ScheduleLockResult(success=False, blocked=True, reason="TIMESTAMPS_MUST_BE_TIMEZONE_AWARE")

        if target_start <= now_utc:
            return ScheduleLockResult(success=False, blocked=True, reason="TARGET_START_MUST_BE_IN_FUTURE")

        duration = target_end - target_start
        if duration != timedelta(hours=48):
            return ScheduleLockResult(
                success=False,
                blocked=True,
                reason=f"Schedule duration must be exactly 48 hours; got {duration.total_seconds() / 3600.0}h",
            )

        exec_repo = ExternalExecutionRepository(session)
        execution = exec_repo.get_by_experiment_id(experiment_id)
        if execution is None or not execution.adset_id:
            return ScheduleLockResult(success=False, blocked=True, reason="ADSET_NOT_FOUND")

        if adapter is None:
            adapter = MetaMarketingApiAdapter(ad_account_id=execution.external_account_id)

        try:
            adapter.update_adset_schedule(execution.adset_id, start_time=target_start, end_time=target_end)
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.SCHEDULE_LOCKED.value,
                    tier="adset",
                    new_resource_id=execution.adset_id,
                    reason=f"Locked exact 48.0h schedule: {target_start.isoformat()} to {target_end.isoformat()}",
                )
            )
            return ScheduleLockResult(
                success=True,
                start_time=target_start,
                end_time=target_end,
                duration_hours=48.0,
            )
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            return ScheduleLockResult(
                success=False,
                blocked=False,
                reason=f"Error locking schedule on Meta: {safe_err}",
            )

    @classmethod
    def activate_recovery_flight(
        cls,
        session: Session,
        experiment_id: UUID,
        adapter: MetaMarketingApiAdapter | None = None,
        safe_mode: bool | None = None,
        explicit_auth: bool = False,
        original_adset_id: str | None = None,
        auto_commit: bool = True,
    ) -> ActivationResult:
        """Phase 2c: Explicitly activate Campaign, Replacement Ad Set, and Replacement Ad.

        Invariants:
        1. SAFE_MODE must be False (explicitly un-gated for authorized activation).
        2. Explicit human operator authorization is strictly required.
        3. Experiment must exist and be in APPROVED status.
        4. Actual spend on financial ledger must be exactly ₹0.00.
        5. Allocated budget must be exactly ₹200.00.
        6. Replacement ad must be verified as APPROVED by Meta ad review.
        7. Replacement ad set schedule must be verified as in the future and exactly 48.0h.
        8. Replacement ad set must NOT be the original expired ad set (original preserved PAUSED).
        9. Sequential activation:
           a. Campaign: unpause -> verify ACTIVE.
           b. Replacement Ad Set: unpause -> verify ACTIVE.
           c. Replacement Ad: unpause -> verify ACTIVE.
        10. Verify original ad set remains PAUSED.
        11. All status transitions recorded immutably in ExternalExecutionEvent.
        12. Retries are idempotent. Partial activation returns is_partially_activated=True.
        13. Does NOT mark experiment RUNNING (remains APPROVED until impressions/spend verified).
        """
        effective_safe_mode = is_safe_mode() if safe_mode is None else safe_mode
        if effective_safe_mode:
            return ActivationResult(success=False, blocked=True, reason="SAFE_MODE_ENABLED")

        if not explicit_auth:
            return ActivationResult(success=False, blocked=True, reason="EXPLICIT_AUTHORIZATION_REQUIRED")

        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return ActivationResult(success=False, blocked=True, reason="EXPERIMENT_NOT_FOUND")

        if exp_orm.status != ExperimentStatus.APPROVED.value:
            return ActivationResult(
                success=False,
                blocked=True,
                reason=f"Experiment in '{exp_orm.status}' status; expected APPROVED",
            )

        cap_repo = CapitalRepository(session, auto_commit=False)
        actual_spend = cap_repo.get_experiment_actual_spend(experiment_id)
        if actual_spend > Decimal("0.00"):
            return ActivationResult(
                success=False,
                blocked=True,
                reason="ACTUAL_SPEND_EXCEEDS_ZERO",
            )

        if exp_orm.allocated_budget != Decimal("200.00"):
            return ActivationResult(
                success=False,
                blocked=True,
                reason=f"Allocated budget is {exp_orm.allocated_budget}, expected 200.00",
            )

        exec_repo = ExternalExecutionRepository(session, auto_commit=auto_commit)
        execution = exec_repo.get_by_experiment_id(experiment_id)
        if execution is None or not execution.adset_id:
            return ActivationResult(success=False, blocked=True, reason="ADSET_NOT_FOUND")
        if not execution.ad_id:
            return ActivationResult(success=False, blocked=True, reason="AD_NOT_FOUND")
        if not execution.campaign_id:
            return ActivationResult(success=False, blocked=True, reason="CAMPAIGN_NOT_FOUND")

        # Guard: Ensure we have verified original ad set lineage and never activate it
        superseded_events = [
            e for e in exec_repo.get_events(execution.id)
            if e.event_type == ExternalExecutionEventType.RESOURCE_SUPERSEDED.value
        ]
        orig_id = original_adset_id or (superseded_events[0].old_resource_id if superseded_events else None)
        if not orig_id:
            return ActivationResult(
                success=False,
                blocked=True,
                reason="ORIGINAL_ADSET_LINEAGE_NOT_VERIFIED",
                campaign_id=execution.campaign_id,
                adset_id=execution.adset_id,
                ad_id=execution.ad_id,
            )

        if execution.adset_id == orig_id:
            return ActivationResult(
                success=False,
                blocked=True,
                reason="CANNOT_ACTIVATE_SUPERSEDED_ORIGINAL_ADSET",
                campaign_id=execution.campaign_id,
                adset_id=execution.adset_id,
                ad_id=execution.ad_id,
            )

        if adapter is None:
            adapter = MetaMarketingApiAdapter(ad_account_id=execution.external_account_id)

        # Pre-activation check 1: Meta ad review approval
        try:
            review_status = adapter.get_ad_review_status(execution.ad_id)
            if not review_status.get("is_approved", False):
                eff = review_status.get("effective_status", "UNKNOWN")
                return ActivationResult(
                    success=False,
                    blocked=True,
                    reason=f"AD_NOT_APPROVED: effective_status={eff}",
                    campaign_id=execution.campaign_id,
                    adset_id=execution.adset_id,
                    ad_id=execution.ad_id,
                )
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            return ActivationResult(
                success=False,
                blocked=False,
                reason=f"Failed to verify ad review status: {safe_err}",
                campaign_id=execution.campaign_id,
                adset_id=execution.adset_id,
                ad_id=execution.ad_id,
            )

        # Pre-activation check 2: Future 48.0-hour schedule
        try:
            adset_meta = adapter.get_adset(execution.adset_id)
            st_str = adset_meta.get("start_time")
            et_str = adset_meta.get("end_time")
            if not st_str or not et_str:
                return ActivationResult(
                    success=False,
                    blocked=True,
                    reason="SCHEDULE_NOT_CONFIGURED",
                    campaign_id=execution.campaign_id,
                    adset_id=execution.adset_id,
                    ad_id=execution.ad_id,
                )
            st = datetime.fromisoformat(st_str.replace("Z", "+00:00"))
            et = datetime.fromisoformat(et_str.replace("Z", "+00:00"))
            now_utc = datetime.now(timezone.utc)
            if et <= now_utc:
                return ActivationResult(
                    success=False,
                    blocked=True,
                    reason="SCHEDULE_EXPIRED",
                    campaign_id=execution.campaign_id,
                    adset_id=execution.adset_id,
                    ad_id=execution.ad_id,
                )
            duration_hrs = (et - st).total_seconds() / 3600.0
            if abs(duration_hrs - 48.0) > 0.05:
                return ActivationResult(
                    success=False,
                    blocked=True,
                    reason=f"SCHEDULE_DURATION_INVALID: {duration_hrs:.2f}h expected 48.0h",
                    campaign_id=execution.campaign_id,
                    adset_id=execution.adset_id,
                    ad_id=execution.ad_id,
                )
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            return ActivationResult(
                success=False,
                blocked=False,
                reason=f"Failed to verify ad set schedule: {safe_err}",
                campaign_id=execution.campaign_id,
                adset_id=execution.adset_id,
                ad_id=execution.ad_id,
            )

        # Pre-activation check 3: Original ad set invariant verification (BEFORE any activation calls)
        try:
            orig_meta = adapter.get_adset(orig_id)
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.ACTIVATION_FAILED.value,
                    tier="adset",
                    old_resource_id=orig_id,
                    reason=f"Failed to inspect original ad set invariant: {safe_err}",
                )
            )
            return ActivationResult(
                success=False,
                blocked=True,
                reason=f"ORIGINAL_ADSET_INSPECTION_FAILED: {safe_err}",
                campaign_id=execution.campaign_id,
                adset_id=execution.adset_id,
                ad_id=execution.ad_id,
            )

        orig_status = str(orig_meta.get("status", "")).upper()
        if orig_status == "ACTIVE":
            pause_err = None
            try:
                adapter.pause_adset(orig_id)
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                        tier="adset",
                        new_resource_id=orig_id,
                        reason="Original ad set was unexpectedly ACTIVE; emergency re-paused",
                    )
                )
            except Exception as pe:
                pause_err = _sanitize_error(str(pe))
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                        tier="adset",
                        new_resource_id=orig_id,
                        reason=f"Original ad set was unexpectedly ACTIVE; emergency re-pause failed: {pause_err}",
                    )
                )

            reason_msg = "ORIGINAL_ADSET_UNEXPECTEDLY_ACTIVE"
            if pause_err:
                reason_msg = f"ORIGINAL_ADSET_UNEXPECTEDLY_ACTIVE: Emergency re-pause failed: {pause_err}"

            return ActivationResult(
                success=False,
                blocked=True,
                reason=reason_msg,
                campaign_id=execution.campaign_id,
                adset_id=execution.adset_id,
                ad_id=execution.ad_id,
                is_partially_activated=False,
            )

        campaign_id = execution.campaign_id
        adset_id = execution.adset_id
        ad_id = execution.ad_id

        campaign_status = "UNKNOWN"
        adset_status = "UNKNOWN"
        ad_status = "UNKNOWN"

        # Tier 1: Campaign activation
        try:
            camp_meta = adapter.get_campaign(campaign_id)
            campaign_status = camp_meta.get("status", "PAUSED")
            if campaign_status != "ACTIVE":
                adapter.unpause_campaign(campaign_id)
                camp_meta_after = adapter.get_campaign(campaign_id)
                campaign_status = camp_meta_after.get("status", "ACTIVE")
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.ACTIVATED.value,
                        tier="campaign",
                        new_resource_id=campaign_id,
                        reason="Campaign explicitly activated for recovery flight",
                    )
                )
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.ACTIVATION_FAILED.value,
                    tier="campaign",
                    new_resource_id=campaign_id,
                    reason=f"Failed to unpause campaign: {safe_err}",
                )
            )
            return ActivationResult(
                success=False,
                blocked=False,
                reason=f"Failed to unpause campaign: {safe_err}",
                campaign_id=campaign_id,
                campaign_status=campaign_status,
                adset_id=adset_id,
                adset_status=adset_status,
                ad_id=ad_id,
                ad_status=ad_status,
                is_partially_activated=False,
            )

        # Tier 2: Replacement Ad Set activation
        try:
            adset_meta = adapter.get_adset(adset_id)
            adset_status = adset_meta.get("status", "PAUSED")
            if adset_status != "ACTIVE":
                adapter.unpause_adset(adset_id)
                adset_meta_after = adapter.get_adset(adset_id)
                adset_status = adset_meta_after.get("status", "ACTIVE")
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.ACTIVATED.value,
                        tier="adset",
                        new_resource_id=adset_id,
                        reason="Replacement ad set explicitly activated for live delivery",
                    )
                )
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.ACTIVATION_FAILED.value,
                    tier="adset",
                    new_resource_id=adset_id,
                    reason=f"Failed to unpause ad set: {safe_err}",
                )
            )
            return ActivationResult(
                success=False,
                blocked=False,
                reason=f"Failed to unpause ad set: {safe_err}",
                campaign_id=campaign_id,
                campaign_status=campaign_status,
                adset_id=adset_id,
                adset_status=adset_status,
                ad_id=ad_id,
                ad_status=ad_status,
                is_partially_activated=(campaign_status == "ACTIVE"),
            )

        # Tier 3: Replacement Ad activation
        try:
            current_ad_meta = adapter.get_ad(ad_id)
            ad_status = current_ad_meta.get("status", "PAUSED")
            if ad_status != "ACTIVE":
                adapter.unpause_ad(ad_id)
                ad_meta_after = adapter.get_ad(ad_id)
                ad_status = ad_meta_after.get("status", "ACTIVE")
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.ACTIVATED.value,
                        tier="ad",
                        new_resource_id=ad_id,
                        reason="Replacement ad explicitly activated for live delivery",
                    )
                )
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.ACTIVATION_FAILED.value,
                    tier="ad",
                    new_resource_id=ad_id,
                    reason=f"Failed to unpause ad: {safe_err}",
                )
            )
            return ActivationResult(
                success=False,
                blocked=False,
                reason=f"Failed to unpause ad: {safe_err}",
                campaign_id=campaign_id,
                campaign_status=campaign_status,
                adset_id=adset_id,
                adset_status=adset_status,
                ad_id=ad_id,
                ad_status=ad_status,
                is_partially_activated=(campaign_status == "ACTIVE" or adset_status == "ACTIVE"),
            )

        # Final verification: All three tiers must be ACTIVE
        if campaign_status == "ACTIVE" and adset_status == "ACTIVE" and ad_status == "ACTIVE":
            execution.status = ExternalExecutionStatus.ACTIVATED
            execution = exec_repo.save(execution)
            return ActivationResult(
                success=True,
                campaign_id=campaign_id,
                campaign_status=campaign_status,
                adset_id=adset_id,
                adset_status=adset_status,
                ad_id=ad_id,
                ad_status=ad_status,
                is_partially_activated=False,
            )
        else:
            return ActivationResult(
                success=False,
                blocked=False,
                reason="Activation verification failed: one or more tiers did not reach ACTIVE status",
                campaign_id=campaign_id,
                campaign_status=campaign_status,
                adset_id=adset_id,
                adset_status=adset_status,
                ad_id=ad_id,
                ad_status=ad_status,
                is_partially_activated=True,
            )

    @classmethod
    def verify_delivery_started(
        cls,
        session: Session,
        experiment_id: UUID,
        impressions: int = 0,
        spend: Decimal = Decimal("0.00"),
        adapter: MetaMarketingApiAdapter | None = None,
        auto_commit: bool = True,
    ) -> DeliveryVerificationResult:
        """Phase 2d: Mark experiment RUNNING only when actual ad delivery is verified."""
        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return DeliveryVerificationResult(
                delivery_started=False,
                experiment_status="UNKNOWN",
                reason="EXPERIMENT_NOT_FOUND",
            )

        exec_repo = ExternalExecutionRepository(session, auto_commit=auto_commit)
        execution = exec_repo.get_by_experiment_id(experiment_id)
        if execution is None:
            return DeliveryVerificationResult(
                delivery_started=False,
                experiment_status=exp_orm.status,
                reason="EXECUTION_RECORD_NOT_FOUND",
            )

        # Enforce that execution must have been explicitly ACTIVATED before delivery can be verified
        if execution.status not in (ExternalExecutionStatus.ACTIVATED, ExternalExecutionStatus.DEPLOYED):
            return DeliveryVerificationResult(
                delivery_started=False,
                experiment_status=exp_orm.status,
                reason=f"Execution status is '{execution.status}'; must be ACTIVATED before delivery can be verified",
            )

        # Delivery verified if impressions > 0 or spend > 0
        if impressions > 0 or spend > Decimal("0.00"):
            start_res = ExperimentExecutionService.start(session, experiment_id, auto_commit=auto_commit)
            if not start_res.is_successful:
                return DeliveryVerificationResult(
                    delivery_started=False,
                    experiment_status=exp_orm.status,
                    reason=f"Failed to transition experiment to RUNNING: {start_res.rejection_reason}",
                )

            execution.status = ExternalExecutionStatus.DEPLOYED
            execution = exec_repo.save(execution)
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.DELIVERY_VERIFIED.value,
                    tier="orchestration",
                    reason=f"Delivery verified: impressions={impressions}, spend={spend}",
                )
            )
            return DeliveryVerificationResult(
                delivery_started=True,
                experiment_status=ExperimentStatus.RUNNING.value,
                actual_start=exp_orm.actual_start,
                impressions=impressions,
                spend=spend,
            )

        return DeliveryVerificationResult(
            delivery_started=False,
            experiment_status=exp_orm.status,
            impressions=impressions,
            spend=spend,
            reason="NO_IMPRESSIONS_OR_SPEND_RECORDED_YET",
        )

    @classmethod
    def emergency_pause_adset(
        cls,
        session: Session,
        experiment_id: UUID,
        adapter: MetaMarketingApiAdapter | None = None,
        safe_mode: bool | None = None,
        explicit_auth: bool = False,
        original_adset_id: str | None = None,
        auto_commit: bool = True,
    ) -> EmergencyStopResult:
        """Coordinated multi-tier emergency stop for an experiment's external Meta resources.

        Pause order:
        1. Parent campaign (if ownership verified exclusively to this experiment).
        2. Replacement ad.
        3. Replacement ad set.
        4. Original ad set (if active or invariant violated).

        Performs read-back verification on all targets. Never silently swallows exceptions.
        """
        effective_safe_mode = is_safe_mode() if safe_mode is None else safe_mode
        if effective_safe_mode:
            return EmergencyStopResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="SAFE_MODE_ENABLED",
            )

        if not explicit_auth:
            return EmergencyStopResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="EXPLICIT_AUTHORIZATION_REQUIRED",
            )

        exec_repo = ExternalExecutionRepository(session, auto_commit=auto_commit)
        execution = exec_repo.get_by_experiment_id(experiment_id)
        if execution is None or not execution.adset_id:
            return EmergencyStopResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="ADSET_NOT_FOUND",
            )

        if adapter is None:
            adapter = MetaMarketingApiAdapter(ad_account_id=execution.external_account_id)

        superseded_events = [
            e for e in exec_repo.get_events(execution.id)
            if e.event_type == ExternalExecutionEventType.RESOURCE_SUPERSEDED.value
        ]
        orig_id = original_adset_id or (superseded_events[0].old_resource_id if superseded_events else None)

        unresolved_scopes: list[str] = []
        camp_status: ResourcePauseStatus | None = None
        ad_status: ResourcePauseStatus | None = None
        adset_status: ResourcePauseStatus | None = None
        orig_status: ResourcePauseStatus | None = None

        # Tier 1: Parent Campaign (verify exclusive ownership before pausing)
        if execution.campaign_id:
            camp_status = ResourcePauseStatus(
                resource_type="campaign",
                resource_id=execution.campaign_id,
            )
            camp_ownership_verified = False
            try:
                camp_meta = adapter.get_campaign(execution.campaign_id)
                camp_name = str(camp_meta.get("name", ""))
                expected_prefix = f"VB-EXP-{experiment_id}"
                if camp_name.startswith(expected_prefix):
                    camp_ownership_verified = True
                else:
                    unresolved_scopes.append(
                        f"Campaign '{execution.campaign_id}' ownership unverified: name '{camp_name}' does not match expected prefix '{expected_prefix}'"
                    )
                    camp_status.error = "Ownership unverified"
            except Exception as e:
                safe_err = _sanitize_error(str(e))
                unresolved_scopes.append(
                    f"Campaign '{execution.campaign_id}' ownership check failed: {safe_err}"
                )
                camp_status.error = f"Ownership check failed: {safe_err}"

            if camp_ownership_verified:
                camp_status.attempted = True
                try:
                    adapter.pause_campaign(execution.campaign_id)
                    camp_status.paused = True
                    exec_repo.record_event(
                        ExternalExecutionEvent(
                            execution_id=execution.id,
                            event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                            tier="campaign",
                            new_resource_id=execution.campaign_id,
                            reason="Emergency pause executed on campaign",
                        )
                    )
                    camp_rb = adapter.get_campaign(execution.campaign_id)
                    if str(camp_rb.get("status", "")).upper() == "PAUSED":
                        camp_status.verified = True
                    else:
                        camp_status.verified = False
                        camp_status.error = f"Read-back status is {camp_rb.get('status')}, expected PAUSED"
                except Exception as e:
                    safe_err = _sanitize_error(str(e))
                    camp_status.error = safe_err
                    exec_repo.record_event(
                        ExternalExecutionEvent(
                            execution_id=execution.id,
                            event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                            tier="campaign",
                            new_resource_id=execution.campaign_id,
                            reason=f"Emergency pause failed on campaign: {safe_err}",
                        )
                    )

        # Tier 2: Replacement Ad
        if execution.ad_id:
            ad_status = ResourcePauseStatus(
                resource_type="ad",
                resource_id=execution.ad_id,
                attempted=True,
            )
            try:
                adapter.pause_ad(execution.ad_id)
                ad_status.paused = True
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                        tier="ad",
                        new_resource_id=execution.ad_id,
                        reason="Emergency pause executed on active ad",
                    )
                )
                ad_rb = adapter.get_ad(execution.ad_id)
                if str(ad_rb.get("status", "")).upper() == "PAUSED":
                    ad_status.verified = True
                else:
                    ad_status.verified = False
                    ad_status.error = f"Read-back status is {ad_rb.get('status')}, expected PAUSED"
            except Exception as e:
                safe_err = _sanitize_error(str(e))
                ad_status.error = safe_err
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                        tier="ad",
                        new_resource_id=execution.ad_id,
                        reason=f"Emergency pause failed on ad: {safe_err}",
                    )
                )

        # Tier 3: Replacement Ad Set
        adset_status = ResourcePauseStatus(
            resource_type="adset",
            resource_id=execution.adset_id,
            attempted=True,
        )
        try:
            adapter.pause_adset(execution.adset_id)
            adset_status.paused = True
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                    tier="adset",
                    new_resource_id=execution.adset_id,
                    reason="Emergency pause executed on active ad set",
                )
            )
            adset_rb = adapter.get_adset(execution.adset_id)
            if str(adset_rb.get("status", "")).upper() == "PAUSED":
                adset_status.verified = True
            else:
                adset_status.verified = False
                adset_status.error = f"Read-back status is {adset_rb.get('status')}, expected PAUSED"
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            adset_status.error = safe_err
            exec_repo.record_event(
                ExternalExecutionEvent(
                    execution_id=execution.id,
                    event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                    tier="adset",
                    new_resource_id=execution.adset_id,
                    reason=f"Emergency pause failed on ad set: {safe_err}",
                )
            )

        # Tier 4: Original Ad Set (if superseded)
        if orig_id and orig_id != execution.adset_id:
            orig_status = ResourcePauseStatus(
                resource_type="original_adset",
                resource_id=orig_id,
            )
            try:
                orig_meta = adapter.get_adset(orig_id)
                current_orig_status = str(orig_meta.get("status", "")).upper()
                if current_orig_status == "ACTIVE":
                    orig_status.attempted = True
                    adapter.pause_adset(orig_id)
                    orig_status.paused = True
                    exec_repo.record_event(
                        ExternalExecutionEvent(
                            execution_id=execution.id,
                            event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                            tier="adset",
                            new_resource_id=orig_id,
                            reason="Original ad set was ACTIVE; paused during emergency stop",
                        )
                    )
                    orig_rb = adapter.get_adset(orig_id)
                    if str(orig_rb.get("status", "")).upper() == "PAUSED":
                        orig_status.verified = True
                    else:
                        orig_status.verified = False
                        orig_status.error = f"Read-back status is {orig_rb.get('status')}, expected PAUSED"
                else:
                    # Already PAUSED
                    orig_status.paused = True
                    orig_status.verified = True
            except Exception as e:
                safe_err = _sanitize_error(str(e))
                orig_status.error = safe_err
                exec_repo.record_event(
                    ExternalExecutionEvent(
                        execution_id=execution.id,
                        event_type=ExternalExecutionEventType.EMERGENCY_STOP.value,
                        tier="adset",
                        new_resource_id=orig_id,
                        reason=f"Emergency pause / check failed on original ad set: {safe_err}",
                    )
                )

        failures = []
        if camp_status and not camp_status.verified:
            failures.append(f"Campaign {execution.campaign_id}: {camp_status.error or 'not verified'}")
        if ad_status and not ad_status.verified:
            failures.append(f"Ad {execution.ad_id}: {ad_status.error or 'not verified'}")
        if adset_status and not adset_status.verified:
            failures.append(f"AdSet {execution.adset_id}: {adset_status.error or 'not verified'}")
        if orig_status and not orig_status.verified:
            failures.append(f"Original AdSet {orig_id}: {orig_status.error or 'not verified'}")
        if unresolved_scopes:
            failures.extend(unresolved_scopes)

        is_success = len(failures) == 0
        rejection_reason = f"Emergency stop encountered failures: {'; '.join(failures)}" if failures else None

        return EmergencyStopResult(
            is_successful=is_success,
            experiment_id=experiment_id,
            execution=execution,
            rejection_reason=rejection_reason,
            campaign_status=camp_status,
            ad_status=ad_status,
            replacement_adset_status=adset_status,
            original_adset_status=orig_status,
            unresolved_scopes=unresolved_scopes,
        )

    @staticmethod
    def check_spend_thresholds(
        current_spend: Decimal,
        allocated_budget: Decimal = Decimal("200.00"),
    ) -> SpendThresholdStatus:
        """Evaluate spend monitoring thresholds against the ₹200.00 ceiling."""
        warn_thresh = Decimal("180.00")
        breaker_thresh = Decimal("190.00")
        return SpendThresholdStatus(
            current_spend=current_spend,
            allocated_budget=allocated_budget,
            warning_threshold=warn_thresh,
            circuit_breaker_threshold=breaker_thresh,
            is_warning=current_spend >= warn_thresh,
            is_circuit_breaker_triggered=current_spend >= breaker_thresh,
        )


class DispatchResult(BaseModel):
    """Result of an external Meta experiment dispatch operation."""

    is_successful: bool
    experiment_id: UUID
    execution: ExternalExecution | None = None
    experiment: Experiment | None = None
    rejection_reason: str | None = None


class ResourcePauseStatus(BaseModel):
    """Status of an individual resource pause attempt during emergency stop."""

    resource_type: str  # "campaign", "ad", "adset", "original_adset"
    resource_id: str
    attempted: bool = False
    paused: bool = False
    verified: bool = False
    error: str | None = None


class EmergencyStopResult(BaseModel):
    """Result of an emergency pause-and-kill coordination operation."""

    is_successful: bool
    experiment_id: UUID
    execution: ExternalExecution | None = None
    experiment: Experiment | None = None
    rejection_reason: str | None = None
    campaign_status: ResourcePauseStatus | None = None
    ad_status: ResourcePauseStatus | None = None
    replacement_adset_status: ResourcePauseStatus | None = None
    original_adset_status: ResourcePauseStatus | None = None
    unresolved_scopes: list[str] = Field(default_factory=list)


class RecoveryDormantResult(BaseModel):
    """Result of deploying a dormant replacement ad set, creative, and ad."""

    success: bool
    blocked: bool = False
    reason: str | None = None
    experiment_id: UUID
    execution: ExternalExecution | None = None
    adset_id: str | None = None
    creative_id: str | None = None
    ad_id: str | None = None


class ReadinessResult(BaseModel):
    """Result of verifying Meta ad review and pre-activation readiness."""

    is_ready: bool
    ad_approved: bool
    ad_status: str
    effective_status: str
    schedule_is_future: bool
    cbo_disabled: bool
    blockers: list[str] = []


class ScheduleLockResult(BaseModel):
    """Result of locking a genuine 48-hour schedule before activation."""

    success: bool
    blocked: bool = False
    reason: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_hours: float | None = None


class ActivationResult(BaseModel):
    """Result of explicitly activating a flight across Campaign, Ad Set, and Ad tiers."""

    success: bool
    blocked: bool = False
    reason: str | None = None
    campaign_id: str | None = None
    campaign_status: str | None = None
    adset_id: str | None = None
    adset_status: str | None = None
    ad_id: str | None = None
    ad_status: str | None = None
    is_partially_activated: bool = False


class DeliveryVerificationResult(BaseModel):
    """Result of verifying actual ad delivery start on Meta."""

    delivery_started: bool
    experiment_status: str
    actual_start: datetime | None = None
    impressions: int = 0
    spend: Decimal = Decimal("0.00")
    reason: str | None = None


class SpendThresholdStatus(BaseModel):
    """Financial spend monitoring status and circuit-breaker threshold evaluation."""

    current_spend: Decimal
    allocated_budget: Decimal
    warning_threshold: Decimal = Decimal("180.00")
    circuit_breaker_threshold: Decimal = Decimal("190.00")
    is_warning: bool
    is_circuit_breaker_triggered: bool
    disclaimer: str = (
        "Polling circuit breaker provides best-effort automated pause; "
        "it is not a real-time hard platform spend guarantee due to reporting latency."
    )


def _sanitize_error(error_msg: str) -> str:
    """Strip access tokens and sensitive credentials from error strings."""
    cleaned = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer [REDACTED]", str(error_msg))
    cleaned = re.sub(r"access_token=[^&\s]+", "access_token=[REDACTED]", cleaned)
    return cleaned


class MetaExperimentDispatchService:
    """Supervised external dispatch coordinator bridging domain experiments to Meta Marketing API."""

    @classmethod
    def _dispatch_from_gateway(
        cls,
        session: Session,
        experiment_id: UUID,
        spec: MetaExecutionSpecification,
        adapter: MetaMarketingApiAdapter | None = None,
        auto_commit: bool = True,
        safe_mode: bool | None = None,
        *,
        gateway_context: _GatewayInvocationContext,
    ) -> DispatchResult:
        """Execute supervised, sequential dispatch of an approved experiment to Meta.

        Invariants enforced:
        0. Gateway invocation context is validated.
        0b. Defense-in-depth SAFE_MODE check is enforced.
        1. Experiment exists and is in APPROVED status.
        2. Referenced Opportunity exists.
        3. Explicit operator dispatch authorization is True.
        4. Specification experiment_id matches target experiment.
        5. Budget validation passes against remaining allocated and ceiling limits.
        6. Meta account metadata is verified active with 'INR' currency.
        7. Pre-existing local deployment is checked; duplicate full deployment rejected.
        8. Deterministic remote duplicate check prevents creating duplicate campaigns.
        9. Resources created sequentially in PAUSED status with immediate persistence.
        10. On full deployment, handoff to ExperimentExecutionService.start() transitions APPROVED -> RUNNING.
        """
        # 0. Context validation
        if not isinstance(gateway_context, _GatewayInvocationContext):
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Missing or invalid gateway invocation context.",
            )
        if gateway_context.experiment_id != experiment_id:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Gateway invocation context does not match target experiment.",
            )

        # 0b. Defense-in-depth SAFE_MODE check
        effective_safe_mode = is_safe_mode() if safe_mode is None else safe_mode
        if effective_safe_mode:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="SAFE_MODE_ENABLED",
            )

        # 1. Experiment exists
        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Experiment '{experiment_id}' does not exist.",
            )

        # 2. Experiment is APPROVED
        if exp_orm.status != ExperimentStatus.APPROVED.value:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Experiment '{experiment_id}' is in '{exp_orm.status}' status. "
                    f"Only experiments in '{ExperimentStatus.APPROVED.value}' status can be dispatched."
                ),
            )

        # 3. Opportunity exists
        opp_orm = session.get(OpportunityORM, exp_orm.opportunity_id)
        if opp_orm is None:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Referenced Opportunity '{exp_orm.opportunity_id}' does not exist.",
            )

        # 4. Explicit operator authorization required
        if not spec.explicit_dispatch_authorized:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Explicit operator dispatch authorization is required before dispatch.",
            )

        # 5. Specification experiment_id matches target
        if spec.experiment_id != experiment_id:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Specification experiment_id '{spec.experiment_id}' "
                    f"does not match target experiment '{experiment_id}'."
                ),
            )

        # 6 & 7. Authoritative financial ledger actual spend & budget invariant check
        cap_repo = CapitalRepository(session, auto_commit=False)
        actual_spend = cap_repo.get_experiment_actual_spend(experiment_id)
        try:
            spec.validate_pre_dispatch(
                allocated_budget=exp_orm.allocated_budget,
                max_allowed_spend=exp_orm.max_allowed_spend,
                actual_spend=actual_spend,
            )
        except ValueError as e:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Pre-dispatch budget validation failed: {e}",
            )

        # Ensure adapter exists
        if adapter is None:
            adapter = MetaMarketingApiAdapter(ad_account_id=spec.ad_account_id)

        # 8. Meta account metadata verification (read-only)
        try:
            account_meta = adapter.get_account_metadata(spec.ad_account_id)
            if not account_meta.is_active:
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    rejection_reason=(
                        f"Meta Ad Account '{spec.ad_account_id}' is not active "
                        f"(account_status={account_meta.account_status})."
                    ),
                )
            if account_meta.currency != "INR":
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    rejection_reason=(
                        f"Meta Ad Account currency '{account_meta.currency}' is invalid; expected 'INR'."
                    ),
                )
        except Exception as e:
            safe_err = _sanitize_error(str(e))
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Failed to verify Meta Ad Account metadata: {safe_err}",
            )

        # 9. Local ExternalExecution state check
        exec_repo = ExternalExecutionRepository(session, auto_commit=auto_commit)
        execution = exec_repo.get_by_experiment_id(experiment_id)

        if execution is not None and execution.status == ExternalExecutionStatus.DEPLOYED:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                execution=execution,
                rejection_reason="Experiment is already fully deployed.",
            )

        if execution is None:
            execution = exec_repo.create(
                ExternalExecution(
                    experiment_id=experiment_id,
                    channel="meta",
                    external_account_id=spec.ad_account_id,
                    status=ExternalExecutionStatus.PENDING,
                )
            )

        # 10. Step 1: Campaign Creation / Reconciliation
        campaign_name = deterministic_campaign_name(experiment_id)
        if not execution.campaign_id:
            campaign_lookup = adapter.lookup_campaign(campaign_name, ad_account_id=spec.ad_account_id)
            if campaign_lookup.status == ReconciliationStatus.FOUND_EXACT:
                execution.campaign_id = campaign_lookup.resource_id
                execution.status = ExternalExecutionStatus.PARTIAL_CAMPAIGN
                execution.last_error = None
                execution = exec_repo.save(execution)
            elif campaign_lookup.status == ReconciliationStatus.NOT_FOUND:
                try:
                    c_id = adapter.create_campaign(
                        name=campaign_name,
                        objective=spec.campaign_objective,
                        special_ad_categories=spec.special_ad_categories,
                        status=spec.status,
                        ad_account_id=spec.ad_account_id,
                    )
                    execution.campaign_id = c_id
                    execution.status = ExternalExecutionStatus.PARTIAL_CAMPAIGN
                    execution.last_error = None
                    execution = exec_repo.save(execution)
                except MetaApiTimeoutError as e:
                    execution.status = ExternalExecutionStatus.TIMEOUT
                    execution.last_error = f"Timeout creating campaign: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
                except Exception as e:
                    execution.status = ExternalExecutionStatus.FAILED
                    execution.last_error = f"Error creating campaign: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
            elif campaign_lookup.status == ReconciliationStatus.AMBIGUOUS:
                execution.status = ExternalExecutionStatus.FAILED
                execution.last_error = f"Ambiguous remote campaign state: {_sanitize_error(campaign_lookup.error_message or '')}"
                execution = exec_repo.save(execution)
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=execution.last_error,
                )
            else:  # UNKNOWN
                is_timeout = "timeout" in (campaign_lookup.error_message or "").lower()
                execution.status = ExternalExecutionStatus.TIMEOUT if is_timeout else ExternalExecutionStatus.FAILED
                execution.last_error = f"Cannot determine remote campaign state (UNKNOWN): {_sanitize_error(campaign_lookup.error_message or '')}"
                execution = exec_repo.save(execution)
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=execution.last_error,
                )

        # 11. Step 2: Image Upload (if required and not already uploaded)
        if not execution.image_hash:
            if spec.image_hash:
                execution.image_hash = spec.image_hash
                execution = exec_repo.save(execution)
            elif spec.image_asset_path:
                try:
                    img_path = Path(spec.image_asset_path)
                    if not img_path.exists():
                        execution.status = ExternalExecutionStatus.FAILED
                        execution.last_error = f"Image asset file not found: {spec.image_asset_path}"
                        execution = exec_repo.save(execution)
                        return DispatchResult(
                            is_successful=False,
                            experiment_id=experiment_id,
                            execution=execution,
                            rejection_reason=execution.last_error,
                        )
                    file_bytes = img_path.read_bytes()
                    img_hash = adapter.upload_image(
                        file_bytes=file_bytes,
                        filename=img_path.name,
                        ad_account_id=spec.ad_account_id,
                    )
                    execution.image_hash = img_hash
                    execution.last_error = None
                    execution = exec_repo.save(execution)
                except MetaApiTimeoutError as e:
                    execution.status = ExternalExecutionStatus.TIMEOUT
                    execution.last_error = f"Timeout uploading image: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
                except Exception as e:
                    execution.status = ExternalExecutionStatus.FAILED
                    execution.last_error = f"Error uploading image: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )

        # 12. Step 3: Ad Set Creation / Reconciliation
        if not execution.adset_id:
            adset_name = deterministic_adset_name(experiment_id)
            paise = inr_to_paise(spec.authorized_budget)
            adset_lookup = adapter.lookup_adset(
                campaign_id=execution.campaign_id,  # type: ignore[arg-type]
                adset_name=adset_name,
                expected_lifetime_budget_paise=paise,
            )
            if adset_lookup.status == ReconciliationStatus.FOUND_EXACT:
                execution.adset_id = adset_lookup.resource_id
                execution.status = ExternalExecutionStatus.PARTIAL_ADSET
                execution.last_error = None
                execution = exec_repo.save(execution)
            elif adset_lookup.status == ReconciliationStatus.NOT_FOUND:
                try:
                    adset_id = adapter.create_adset(
                        campaign_id=execution.campaign_id,  # type: ignore[arg-type]
                        name=adset_name,
                        lifetime_budget_paise=paise,
                        end_time=spec.end_time,  # type: ignore[arg-type]
                        start_time=spec.start_time,
                        countries=spec.countries,
                        age_min=spec.age_min,
                        age_max=spec.age_max,
                        status=spec.status,
                        ad_account_id=spec.ad_account_id,
                    )
                    execution.adset_id = adset_id
                    execution.status = ExternalExecutionStatus.PARTIAL_ADSET
                    execution.last_error = None
                    execution = exec_repo.save(execution)
                except MetaApiTimeoutError as e:
                    execution.status = ExternalExecutionStatus.TIMEOUT
                    execution.last_error = f"Timeout creating ad set: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
                except Exception as e:
                    execution.status = ExternalExecutionStatus.FAILED
                    execution.last_error = f"Error creating ad set: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
            elif adset_lookup.status == ReconciliationStatus.AMBIGUOUS:
                execution.status = ExternalExecutionStatus.FAILED
                execution.last_error = f"Ambiguous remote ad set state: {_sanitize_error(adset_lookup.error_message or '')}"
                execution = exec_repo.save(execution)
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=execution.last_error,
                )
            else:  # UNKNOWN
                is_timeout = "timeout" in (adset_lookup.error_message or "").lower()
                execution.status = ExternalExecutionStatus.TIMEOUT if is_timeout else ExternalExecutionStatus.FAILED
                execution.last_error = f"Cannot determine remote ad set state (UNKNOWN): {_sanitize_error(adset_lookup.error_message or '')}"
                execution = exec_repo.save(execution)
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=execution.last_error,
                )

        # 13. Step 4: Creative Creation / Reconciliation
        if not execution.creative_id:
            creative_name = deterministic_creative_name(experiment_id)
            creative_lookup = adapter.lookup_creative(
                creative_name=creative_name,
                page_id=spec.page_id,
                image_hash=execution.image_hash or "",
                destination_url=spec.destination_url,
                ad_account_id=spec.ad_account_id,
            )
            if creative_lookup.status == ReconciliationStatus.FOUND_EXACT:
                execution.creative_id = creative_lookup.resource_id
                execution.status = ExternalExecutionStatus.PARTIAL_CREATIVE
                execution.last_error = None
                execution = exec_repo.save(execution)
            elif creative_lookup.status == ReconciliationStatus.NOT_FOUND:
                try:
                    creative_id = adapter.create_creative(
                        name=creative_name,
                        page_id=spec.page_id,
                        link=spec.destination_url,
                        message=spec.primary_text,
                        headline=spec.headline,
                        image_hash=execution.image_hash or "",
                        call_to_action=spec.call_to_action,
                        ad_account_id=spec.ad_account_id,
                    )
                    execution.creative_id = creative_id
                    execution.status = ExternalExecutionStatus.PARTIAL_CREATIVE
                    execution.last_error = None
                    execution = exec_repo.save(execution)
                except MetaApiTimeoutError as e:
                    execution.status = ExternalExecutionStatus.TIMEOUT
                    execution.last_error = f"Timeout creating creative: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
                except Exception as e:
                    execution.status = ExternalExecutionStatus.FAILED
                    execution.last_error = f"Error creating creative: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
            elif creative_lookup.status == ReconciliationStatus.AMBIGUOUS:
                execution.status = ExternalExecutionStatus.FAILED
                execution.last_error = f"Ambiguous remote creative state: {_sanitize_error(creative_lookup.error_message or '')}"
                execution = exec_repo.save(execution)
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=execution.last_error,
                )
            else:  # UNKNOWN
                is_timeout = "timeout" in (creative_lookup.error_message or "").lower()
                execution.status = ExternalExecutionStatus.TIMEOUT if is_timeout else ExternalExecutionStatus.FAILED
                execution.last_error = f"Cannot determine remote creative state (UNKNOWN): {_sanitize_error(creative_lookup.error_message or '')}"
                execution = exec_repo.save(execution)
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=execution.last_error,
                )

        # 14. Step 5: Ad Creation / Reconciliation
        if not execution.ad_id:
            ad_name = deterministic_ad_name(experiment_id)
            ad_lookup = adapter.lookup_ad(
                adset_id=execution.adset_id,  # type: ignore[arg-type]
                ad_name=ad_name,
                creative_id=execution.creative_id,  # type: ignore[arg-type]
            )
            if ad_lookup.status == ReconciliationStatus.FOUND_EXACT:
                execution.ad_id = ad_lookup.resource_id
                execution.status = ExternalExecutionStatus.DEPLOYED
                execution.last_error = None
                execution = exec_repo.save(execution)
            elif ad_lookup.status == ReconciliationStatus.NOT_FOUND:
                try:
                    ad_id = adapter.create_ad(
                        name=ad_name,
                        adset_id=execution.adset_id,  # type: ignore[arg-type]
                        creative_id=execution.creative_id,  # type: ignore[arg-type]
                        status=spec.status,
                        ad_account_id=spec.ad_account_id,
                    )
                    execution.ad_id = ad_id
                    execution.status = ExternalExecutionStatus.DEPLOYED
                    execution.last_error = None
                    execution = exec_repo.save(execution)
                except MetaApiTimeoutError as e:
                    execution.status = ExternalExecutionStatus.TIMEOUT
                    execution.last_error = f"Timeout creating ad: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
                except Exception as e:
                    execution.status = ExternalExecutionStatus.FAILED
                    execution.last_error = f"Error creating ad: {_sanitize_error(str(e))}"
                    execution = exec_repo.save(execution)
                    return DispatchResult(
                        is_successful=False,
                        experiment_id=experiment_id,
                        execution=execution,
                        rejection_reason=execution.last_error,
                    )
            elif ad_lookup.status == ReconciliationStatus.AMBIGUOUS:
                execution.status = ExternalExecutionStatus.FAILED
                execution.last_error = f"Ambiguous remote ad state: {_sanitize_error(ad_lookup.error_message or '')}"
                execution = exec_repo.save(execution)
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=execution.last_error,
                )
            else:  # UNKNOWN
                is_timeout = "timeout" in (ad_lookup.error_message or "").lower()
                execution.status = ExternalExecutionStatus.TIMEOUT if is_timeout else ExternalExecutionStatus.FAILED
                execution.last_error = f"Cannot determine remote ad state (UNKNOWN): {_sanitize_error(ad_lookup.error_message or '')}"
                execution = exec_repo.save(execution)
                return DispatchResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=execution.last_error,
                )

        # 15. Success Handoff: All 4 resources deployed; start internal experiment lifecycle
        start_res = ExperimentExecutionService.start(session, experiment_id, auto_commit=auto_commit)
        if not start_res.is_successful:
            return DispatchResult(
                is_successful=False,
                experiment_id=experiment_id,
                execution=execution,
                experiment=start_res.experiment,
                rejection_reason=(
                    f"External deployment succeeded (status: DEPLOYED), but internal lifecycle start failed: "
                    f"{start_res.rejection_reason}"
                ),
            )

        return DispatchResult(
            is_successful=True,
            experiment_id=experiment_id,
            execution=execution,
            experiment=start_res.experiment,
        )

    @classmethod
    def _emergency_stop(
        cls,
        session: Session,
        experiment_id: UUID,
        reason: str,
        adapter: MetaMarketingApiAdapter | None = None,
        auto_commit: bool = True,
    ) -> EmergencyStopResult:
        """Attempt to pause active external Meta campaign before terminating internal experiment.

        If external pause fails:
        - preserves external error on ExternalExecution record
        - does NOT falsely claim resource is paused
        - does NOT mark internal experiment KILLED
        - leaves experiment in its existing active state for human intervention
        """
        exec_repo = ExternalExecutionRepository(session, auto_commit=auto_commit)
        execution = exec_repo.get_by_experiment_id(experiment_id)

        # If external deployment exists and has a campaign_id, attempt to pause it on Meta first
        if execution is not None and execution.campaign_id:
            if adapter is None:
                adapter = MetaMarketingApiAdapter(ad_account_id=execution.external_account_id)
            try:
                adapter.pause_campaign(execution.campaign_id)
                execution.last_error = None
                execution = exec_repo.save(execution)
            except Exception as e:
                safe_err = _sanitize_error(str(e))
                execution.last_error = f"Emergency pause failed: {safe_err}"
                execution = exec_repo.save(execution)
                return EmergencyStopResult(
                    is_successful=False,
                    experiment_id=experiment_id,
                    execution=execution,
                    rejection_reason=(
                        f"External Meta campaign pause failed: {safe_err}. "
                        "Experiment remains active to prevent untracked spending."
                    ),
                )

        # Only after external pause succeeds (or if no external deployment exists), invoke internal kill
        kill_res = ExperimentExecutionService.kill(
            session,
            experiment_id,
            reason=reason,
            auto_commit=auto_commit,
        )
        if not kill_res.is_successful:
            return EmergencyStopResult(
                is_successful=False,
                experiment_id=experiment_id,
                execution=execution,
                experiment=kill_res.experiment,
                rejection_reason=f"External pause succeeded, but internal kill failed: {kill_res.rejection_reason}",
            )

        return EmergencyStopResult(
            is_successful=True,
            experiment_id=experiment_id,
            execution=execution,
            experiment=kill_res.experiment,
        )

    # Alias for backwards compatibility
    emergency_stop = _emergency_stop

