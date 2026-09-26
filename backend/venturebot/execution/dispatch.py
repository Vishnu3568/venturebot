"""Meta experiment dispatch orchestration service for VentureBot (Step 39)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, field_validator
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
from venturebot.models.external_execution import ExternalExecution, ExternalExecutionStatus


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


class DispatchResult(BaseModel):
    """Result of an external Meta experiment dispatch operation."""

    is_successful: bool
    experiment_id: UUID
    execution: ExternalExecution | None = None
    experiment: Experiment | None = None
    rejection_reason: str | None = None


class EmergencyStopResult(BaseModel):
    """Result of an emergency pause-and-kill coordination operation."""

    is_successful: bool
    experiment_id: UUID
    execution: ExternalExecution | None = None
    experiment: Experiment | None = None
    rejection_reason: str | None = None


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

