"""Controlled Approval and Capital Allocation Service for VentureBot."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from venturebot.approval.models import ApprovalRequest, ApprovalResult
from venturebot.database.models import ExperimentORM, OpportunityORM
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.models.capital import TransactionType
from venturebot.models.decision import Decision, DecisionOutcome
from venturebot.models.experiment import ExperimentStatus
from venturebot.models.opportunity import OpportunityStatus

STARTING_CAPITAL_CEILING = Decimal("1000.00")


class ExperimentApprovalService:
    """Service governing the transition of Experiments from DRAFT to APPROVED.
    
    Enforces strict capital protection, budget allocation rules, and decision audit trails.
    Does NOT disburse actual funds or create actual spending transactions.
    """

    @classmethod
    def approve(
        cls,
        session: Session,
        request: ApprovalRequest | UUID | None = None,
        *,
        experiment_id: UUID | None = None,
        reason: str | None = None,
        allocated_budget: Decimal | None = None,
        max_allowed_spend: Decimal | None = None,
        auto_commit: bool = True,
    ) -> ApprovalResult:
        """Explicitly approve a DRAFT experiment and allocate committed capital."""
        if isinstance(request, UUID):
            experiment_id = request
            request = None

        if request is None:
            if experiment_id is None:
                raise ValueError("experiment_id is required for approval")
            if reason is None:
                raise ValueError("reason is required for approval")
            request = ApprovalRequest(
                experiment_id=experiment_id,
                reason=reason,
                allocated_budget=allocated_budget,
                max_allowed_spend=max_allowed_spend,
            )

        exp_id = request.experiment_id

        # 1. Verify reason is non-empty
        clean_reason = request.reason.strip()
        if not clean_reason:
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason="Approval requires an explicit, non-empty reason.",
            )

        # 2. Retrieve Experiment
        exp_repo = ExperimentRepository(session, auto_commit=False)
        cap_repo = CapitalRepository(session, auto_commit=False)
        dec_repo = DecisionRepository(session, auto_commit=False)

        exp_orm = session.get(ExperimentORM, exp_id)
        if exp_orm is None:
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason=f"Experiment with ID '{exp_id}' does not exist.",
            )

        # 3. Verify Pre-Approval State (must be DRAFT)
        if exp_orm.status != ExperimentStatus.DRAFT.value:
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason=(
                    f"Experiment '{exp_id}' is in '{exp_orm.status}' status. "
                    f"Only experiments in '{ExperimentStatus.DRAFT.value}' status can be approved."
                ),
            )

        # 4. Verify Opportunity Reference
        opp_orm = session.get(OpportunityORM, exp_orm.opportunity_id)
        if opp_orm is None:
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason=f"Referenced Opportunity '{exp_orm.opportunity_id}' does not exist.",
            )

        # 5. Verify Essential Experiment Fields
        if not (exp_orm.hypothesis and exp_orm.hypothesis.strip()):
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason="Experiment is missing a testable hypothesis.",
            )
        if not (exp_orm.objective and exp_orm.objective.strip()):
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason="Experiment is missing an objective.",
            )
        if not (exp_orm.success_criteria and exp_orm.success_criteria.strip()):
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason="Experiment is missing success criteria.",
            )
        if not (exp_orm.failure_criteria and exp_orm.failure_criteria.strip()):
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason="Experiment is missing failure criteria.",
            )

        # 6. Determine & Validate Budget Parameters
        target_budget = (
            request.allocated_budget
            if request.allocated_budget is not None
            else (
                exp_orm.allocated_budget
                if isinstance(exp_orm.allocated_budget, Decimal)
                else Decimal(str(exp_orm.allocated_budget))
            )
        )
        target_max_spend = (
            request.max_allowed_spend
            if request.max_allowed_spend is not None
            else (
                exp_orm.max_allowed_spend
                if isinstance(exp_orm.max_allowed_spend, Decimal)
                else Decimal(str(exp_orm.max_allowed_spend))
            )
        )

        if target_budget < Decimal("0.00"):
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason=f"Allocated budget cannot be negative (got ₹{target_budget:.2f}).",
            )

        if target_max_spend < target_budget:
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason=(
                    f"max_allowed_spend (₹{target_max_spend:.2f}) cannot be less than "
                    f"allocated_budget (₹{target_budget:.2f})."
                ),
            )

        if target_max_spend > STARTING_CAPITAL_CEILING:
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason=(
                    f"max_allowed_spend (₹{target_max_spend:.2f}) exceeds starting capital "
                    f"pool ceiling (₹{STARTING_CAPITAL_CEILING:.2f})."
                ),
            )

        # 7. Capital Protection Check (Pool Availability)
        available_unallocated = cap_repo.get_available_unallocated_capital()
        if target_budget > available_unallocated:
            current_balance = cap_repo.get_current_balance()
            active_allocations = cap_repo.get_total_active_allocations()
            return ApprovalResult(
                is_approved=False,
                experiment_id=exp_id,
                rejection_reason=(
                    f"Insufficient available capital for allocation: requested ₹{target_budget:.2f}, "
                    f"but available unallocated capital is ₹{available_unallocated:.2f} "
                    f"(current balance: ₹{current_balance:.2f}, committed active allocations: ₹{active_allocations:.2f})."
                ),
            )

        # 8. Apply Updates & Transitions
        exp_orm.status = ExperimentStatus.APPROVED.value
        exp_orm.allocated_budget = target_budget
        exp_orm.max_allowed_spend = target_max_spend

        # Also update Opportunity to approved if it is still discovered/under_review
        if opp_orm.status in (OpportunityStatus.DISCOVERED.value, OpportunityStatus.UNDER_REVIEW.value):
            opp_orm.status = OpportunityStatus.APPROVED.value

        # 9. Record Capital Allocation in Ledger (if budget > 0)
        allocation_tx = None
        if target_budget > Decimal("0.00"):
            allocation_tx = cap_repo.record_transaction(
                experiment_id=exp_id,
                transaction_type=TransactionType.EXPERIMENT_ALLOCATION,
                amount=target_budget,
                description=f"Capital allocation of ₹{target_budget:.2f} for experiment '{exp_id}': {clean_reason}",
            )

        # 10. Record Decision Audit Trail
        decision = dec_repo.create(
            Decision(
                opportunity_id=exp_orm.opportunity_id,
                experiment_id=exp_id,
                outcome=DecisionOutcome.APPROVE,
                reason=clean_reason,
                evidence_summary=f"Allocated budget: ₹{target_budget:.2f}, Max spend ceiling: ₹{target_max_spend:.2f}",
            )
        )

        if auto_commit:
            session.commit()
        else:
            session.flush()

        approved_exp = exp_repo.get(exp_id)

        return ApprovalResult(
            is_approved=True,
            experiment_id=exp_id,
            experiment=approved_exp,
            decision=decision,
            allocation_transaction=allocation_tx,
            rejection_reason=None,
        )

    @classmethod
    def reject(
        cls,
        session: Session,
        experiment_id: UUID,
        reason: str,
        auto_commit: bool = True,
    ) -> ApprovalResult:
        """Explicitly reject a DRAFT experiment, transitioning it to KILLED with audit record."""
        clean_reason = reason.strip()
        if not clean_reason:
            return ApprovalResult(
                is_approved=False,
                experiment_id=experiment_id,
                rejection_reason="Rejection requires an explicit, non-empty reason.",
            )

        exp_repo = ExperimentRepository(session, auto_commit=False)
        dec_repo = DecisionRepository(session, auto_commit=False)

        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return ApprovalResult(
                is_approved=False,
                experiment_id=experiment_id,
                rejection_reason=f"Experiment with ID '{experiment_id}' does not exist.",
            )

        if exp_orm.status != ExperimentStatus.DRAFT.value:
            return ApprovalResult(
                is_approved=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Experiment '{experiment_id}' is in '{exp_orm.status}' status. "
                    f"Only experiments in '{ExperimentStatus.DRAFT.value}' status can be rejected."
                ),
            )

        exp_orm.status = ExperimentStatus.KILLED.value

        decision = dec_repo.create(
            Decision(
                opportunity_id=exp_orm.opportunity_id,
                experiment_id=experiment_id,
                outcome=DecisionOutcome.KILL,
                reason=clean_reason,
                evidence_summary="Experiment proposal rejected during approval review.",
            )
        )

        if auto_commit:
            session.commit()
        else:
            session.flush()

        killed_exp = exp_repo.get(experiment_id)

        return ApprovalResult(
            is_approved=False,
            experiment_id=experiment_id,
            experiment=killed_exp,
            decision=decision,
            allocation_transaction=None,
            rejection_reason=f"Rejected: {clean_reason}",
        )
