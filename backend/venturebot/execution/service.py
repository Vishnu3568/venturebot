"""Controlled Experiment Execution Foundation Service for VentureBot.

Governs internal lifecycle transitions (APPROVED -> RUNNING -> COMPLETED/KILLED)
and controlled spending enforcement against max_allowed_spend.
Does NOT execute real-world actions (no external APIs, ads, scraping, or automated spending).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentORM, OpportunityORM
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.execution.models import ExecutionResult
from venturebot.models.capital import TransactionType
from venturebot.models.decision import Decision, DecisionOutcome
from venturebot.models.experiment import Experiment, ExperimentStatus
from venturebot.models.metrics import ExperimentMetrics


class ExperimentExecutionService:
    """Internal execution lifecycle and spend-control service."""

    @classmethod
    def start(
        cls,
        session: Session,
        experiment_id: UUID,
        auto_commit: bool = True,
    ) -> ExecutionResult:
        """Start an experiment (transition from APPROVED to RUNNING).

        Zero capital transactions are created by starting an experiment.
        """
        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Experiment '{experiment_id}' does not exist.",
            )

        if exp_orm.status != ExperimentStatus.APPROVED.value:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Experiment '{experiment_id}' is in '{exp_orm.status}' status. "
                    f"Only experiments in '{ExperimentStatus.APPROVED.value}' status can be started."
                ),
            )

        opp_orm = session.get(OpportunityORM, exp_orm.opportunity_id)
        if opp_orm is None:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Referenced Opportunity '{exp_orm.opportunity_id}' does not exist.",
            )

        if not (exp_orm.hypothesis and exp_orm.hypothesis.strip()):
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Experiment is missing a testable hypothesis.",
            )
        if not (exp_orm.objective and exp_orm.objective.strip()):
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Experiment is missing an objective.",
            )
        if not (exp_orm.success_criteria and exp_orm.success_criteria.strip()):
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Experiment is missing success criteria.",
            )
        if not (exp_orm.failure_criteria and exp_orm.failure_criteria.strip()):
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Experiment is missing failure criteria.",
            )

        exp_orm.status = ExperimentStatus.RUNNING.value
        if exp_orm.actual_start is None:
            exp_orm.actual_start = datetime.now(timezone.utc)

        if auto_commit:
            session.commit()
        else:
            session.flush()

        exp_repo = ExperimentRepository(session, auto_commit=False)
        running_exp = exp_repo.get(experiment_id)

        return ExecutionResult(
            is_successful=True,
            experiment_id=experiment_id,
            experiment=running_exp,
            rejection_reason=None,
        )

    @classmethod
    def pause(
        cls,
        session: Session,
        experiment_id: UUID,
        reason: str = "",
        auto_commit: bool = True,
    ) -> ExecutionResult:
        """Pause a currently RUNNING experiment."""
        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Experiment '{experiment_id}' does not exist.",
            )

        if exp_orm.status != ExperimentStatus.RUNNING.value:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Experiment '{experiment_id}' is in '{exp_orm.status}' status. "
                    f"Only experiments in '{ExperimentStatus.RUNNING.value}' status can be paused."
                ),
            )

        exp_orm.status = ExperimentStatus.PAUSED.value

        decision = None
        if reason.strip():
            dec_repo = DecisionRepository(session, auto_commit=False)
            decision = dec_repo.create(
                Decision(
                    opportunity_id=exp_orm.opportunity_id,
                    experiment_id=experiment_id,
                    outcome=DecisionOutcome.HOLD,
                    reason=reason.strip(),
                    evidence_summary="Experiment paused during execution.",
                )
            )

        if auto_commit:
            session.commit()
        else:
            session.flush()

        exp_repo = ExperimentRepository(session, auto_commit=False)
        paused_exp = exp_repo.get(experiment_id)

        return ExecutionResult(
            is_successful=True,
            experiment_id=experiment_id,
            experiment=paused_exp,
            decision=decision,
            rejection_reason=None,
        )

    @classmethod
    def resume(
        cls,
        session: Session,
        experiment_id: UUID,
        auto_commit: bool = True,
    ) -> ExecutionResult:
        """Resume a PAUSED experiment back to RUNNING."""
        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Experiment '{experiment_id}' does not exist.",
            )

        if exp_orm.status != ExperimentStatus.PAUSED.value:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Experiment '{experiment_id}' is in '{exp_orm.status}' status. "
                    f"Only experiments in '{ExperimentStatus.PAUSED.value}' status can be resumed."
                ),
            )

        exp_orm.status = ExperimentStatus.RUNNING.value

        if auto_commit:
            session.commit()
        else:
            session.flush()

        exp_repo = ExperimentRepository(session, auto_commit=False)
        running_exp = exp_repo.get(experiment_id)

        return ExecutionResult(
            is_successful=True,
            experiment_id=experiment_id,
            experiment=running_exp,
            rejection_reason=None,
        )

    @classmethod
    def complete(
        cls,
        session: Session,
        experiment_id: UUID,
        reason: str = "",
        decision_outcome: DecisionOutcome | str = DecisionOutcome.SCALE,
        auto_commit: bool = True,
    ) -> ExecutionResult:
        """Complete an experiment (transition from RUNNING/PAUSED to COMPLETED).

        Releases remaining unspent allocation automatically without fake cash transactions.
        """
        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Experiment '{experiment_id}' does not exist.",
            )

        if exp_orm.status not in (ExperimentStatus.RUNNING.value, ExperimentStatus.PAUSED.value):
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Experiment '{experiment_id}' is in '{exp_orm.status}' status. "
                    f"Only experiments in '{ExperimentStatus.RUNNING.value}' or "
                    f"'{ExperimentStatus.PAUSED.value}' status can be completed."
                ),
            )

        exp_orm.status = ExperimentStatus.COMPLETED.value
        exp_orm.actual_end = datetime.now(timezone.utc)

        decision = None
        if reason.strip():
            outcome_val = (
                decision_outcome
                if isinstance(decision_outcome, DecisionOutcome)
                else DecisionOutcome(str(decision_outcome))
            )
            dec_repo = DecisionRepository(session, auto_commit=False)
            decision = dec_repo.create(
                Decision(
                    opportunity_id=exp_orm.opportunity_id,
                    experiment_id=experiment_id,
                    outcome=outcome_val,
                    reason=reason.strip(),
                    evidence_summary="Experiment completed successfully.",
                )
            )

        if auto_commit:
            session.commit()
        else:
            session.flush()

        exp_repo = ExperimentRepository(session, auto_commit=False)
        completed_exp = exp_repo.get(experiment_id)

        return ExecutionResult(
            is_successful=True,
            experiment_id=experiment_id,
            experiment=completed_exp,
            decision=decision,
            rejection_reason=None,
        )

    @classmethod
    def kill(
        cls,
        session: Session,
        experiment_id: UUID,
        reason: str,
        auto_commit: bool = True,
    ) -> ExecutionResult:
        """Kill an experiment during execution (transition from APPROVED/RUNNING/PAUSED to KILLED).

        Requires an explicit human reason. Releases remaining allocation without fake refunds.
        """
        clean_reason = reason.strip()
        if not clean_reason:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Killing an experiment requires an explicit, non-empty reason.",
            )

        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Experiment '{experiment_id}' does not exist.",
            )

        allowed_statuses = (
            ExperimentStatus.APPROVED.value,
            ExperimentStatus.RUNNING.value,
            ExperimentStatus.PAUSED.value,
        )
        if exp_orm.status not in allowed_statuses:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Experiment '{experiment_id}' is in '{exp_orm.status}' status. "
                    f"Only experiments in {allowed_statuses} status can be terminated."
                ),
            )

        exp_orm.status = ExperimentStatus.KILLED.value
        exp_orm.actual_end = datetime.now(timezone.utc)

        dec_repo = DecisionRepository(session, auto_commit=False)
        decision = dec_repo.create(
            Decision(
                opportunity_id=exp_orm.opportunity_id,
                experiment_id=experiment_id,
                outcome=DecisionOutcome.KILL,
                reason=clean_reason,
                evidence_summary="Experiment terminated early.",
            )
        )

        if auto_commit:
            session.commit()
        else:
            session.flush()

        exp_repo = ExperimentRepository(session, auto_commit=False)
        killed_exp = exp_repo.get(experiment_id)

        return ExecutionResult(
            is_successful=True,
            experiment_id=experiment_id,
            experiment=killed_exp,
            decision=decision,
            rejection_reason=None,
        )

    @classmethod
    def record_spend(
        cls,
        session: Session,
        experiment_id: UUID,
        amount: Decimal,
        description: str,
        auto_commit: bool = True,
    ) -> ExecutionResult:
        """Record verified actual spending for an experiment against the financial ledger.

        Enforces:
        - Experiment must exist and be in RUNNING status.
        - Spend amount must be > 0.00.
        - current_actual_spend + amount <= max_allowed_spend.
        - amount <= current_available_balance.
        """
        clean_desc = description.strip()
        if not clean_desc:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason="Spend description is required.",
            )

        if amount <= Decimal("0.00"):
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Spend amount must be strictly positive (got ₹{amount:.2f}).",
            )

        exp_orm = session.get(ExperimentORM, experiment_id)
        if exp_orm is None:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=f"Experiment '{experiment_id}' does not exist.",
            )

        if exp_orm.status != ExperimentStatus.RUNNING.value:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Cannot record spend for experiment in '{exp_orm.status}' status. "
                    f"Experiment must be in '{ExperimentStatus.RUNNING.value}' status to record spend."
                ),
            )

        cap_repo = CapitalRepository(session, auto_commit=False)
        current_spend = cap_repo.get_experiment_actual_spend(experiment_id)
        max_spend = (
            exp_orm.max_allowed_spend
            if isinstance(exp_orm.max_allowed_spend, Decimal)
            else Decimal(str(exp_orm.max_allowed_spend))
        )

        projected_spend = current_spend + amount
        if projected_spend > max_spend:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Spend of ₹{amount:.2f} would cause total spend (₹{projected_spend:.2f}) "
                    f"to exceed max_allowed_spend (₹{max_spend:.2f})."
                ),
            )

        current_balance = cap_repo.get_current_balance()
        if amount > current_balance:
            return ExecutionResult(
                is_successful=False,
                experiment_id=experiment_id,
                rejection_reason=(
                    f"Spend of ₹{amount:.2f} exceeds available liquid cash balance (₹{current_balance:.2f})."
                ),
            )

        spend_tx = cap_repo.record_transaction(
            experiment_id=experiment_id,
            transaction_type=TransactionType.EXPERIMENT_SPEND,
            amount=amount,
            description=clean_desc,
        )

        if auto_commit:
            session.commit()
        else:
            session.flush()

        exp_repo = ExperimentRepository(session, auto_commit=False)
        updated_exp = exp_repo.get(experiment_id)

        return ExecutionResult(
            is_successful=True,
            experiment_id=experiment_id,
            experiment=updated_exp,
            transaction=spend_tx,
            rejection_reason=None,
        )

    @classmethod
    def record_metrics(
        cls,
        session: Session,
        metrics: ExperimentMetrics,
        auto_commit: bool = True,
    ) -> ExperimentMetrics:
        """Record an ExperimentMetrics snapshot. Isolated from financial ledger."""
        from venturebot.measurement.service import ExperimentMeasurementService

        return ExperimentMeasurementService.record_measurement(
            session=session,
            metrics=metrics,
            auto_commit=auto_commit,
        )
