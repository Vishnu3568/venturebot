"""Capital ledger repository and financial calculation service for VentureBot."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from venturebot.database.models import CapitalTransactionORM, ExperimentORM
from venturebot.models.capital import CapitalTransaction, TransactionType
from venturebot.models.experiment import ExperimentStatus

STARTING_CAPITAL_AMOUNT = Decimal("1000.00")
STARTING_CAPITAL_DESCRIPTION = "Initial starting capital ₹1,000.00"


class FinancialSummary(BaseModel):
    """Financial overview designed for internal tracking and future dashboard consumption."""

    starting_capital: Decimal = Field(default=Decimal("0.00"))
    current_balance: Decimal = Field(default=Decimal("0.00"))
    total_inflow: Decimal = Field(default=Decimal("0.00"))
    total_outflow: Decimal = Field(default=Decimal("0.00"))
    total_revenue: Decimal = Field(default=Decimal("0.00"))
    total_cost: Decimal = Field(default=Decimal("0.00"))
    net_profit: Decimal = Field(default=Decimal("0.00"))
    total_allocated: Decimal = Field(default=Decimal("0.00"))
    available_unallocated: Decimal = Field(default=Decimal("0.00"))
    roi: float | None = None

    @property
    def total_experiment_spending(self) -> Decimal:
        """Alias for total_cost to align with experiment spending terminology."""
        return self.total_cost

    @property
    def total_profit_loss(self) -> Decimal:
        """Alias for net_profit to align with profit/loss terminology."""
        return self.net_profit

    @property
    def current_available_capital(self) -> Decimal:
        """Alias for current_balance representing available cash in the capital pool."""
        return self.current_balance


class CapitalRepository:
    """Append-oriented ledger repository for CapitalTransaction records.
    
    Provides auditable financial tracking, invariant checks, starting capital
    initialization, and financial summary calculations.
    """

    def __init__(self, session: Session, auto_commit: bool = True):
        self.session = session
        self.auto_commit = auto_commit

    def initialize_starting_capital(
        self,
        amount: Decimal = STARTING_CAPITAL_AMOUNT,
        description: str = STARTING_CAPITAL_DESCRIPTION,
    ) -> CapitalTransaction:
        """Initialize VentureBot's starting capital as an append-only ledger transaction.

        Idempotent: if an INITIAL_DEPOSIT transaction already exists, returns the
        existing transaction without creating a duplicate.
        """
        stmt = select(CapitalTransactionORM).where(
            CapitalTransactionORM.transaction_type == TransactionType.INITIAL_DEPOSIT.value
        )
        existing = self.session.scalars(stmt).first()
        if existing is not None:
            return self._to_pydantic(existing)

        tx = CapitalTransaction(
            transaction_type=TransactionType.INITIAL_DEPOSIT,
            amount=amount,
            description=description,
        )
        return self.record_transaction(tx)

    def record_transaction(
        self,
        transaction: CapitalTransaction | None = None,
        *,
        transaction_type: TransactionType | str | None = None,
        amount: Decimal | str | float | None = None,
        description: str | None = None,
        experiment_id: UUID | None = None,
    ) -> CapitalTransaction:
        """Record an auditable capital transaction into the ledger.

        Validates all constraints through the canonical CapitalTransaction model.
        Guards against duplicate INITIAL_DEPOSIT transactions.
        """
        if transaction is None:
            transaction = CapitalTransaction(
                transaction_type=transaction_type,  # type: ignore[arg-type]
                amount=amount,  # type: ignore[arg-type]
                description=description,  # type: ignore[arg-type]
                experiment_id=experiment_id,
            )

        if transaction.transaction_type == TransactionType.INITIAL_DEPOSIT:
            stmt = select(CapitalTransactionORM).where(
                CapitalTransactionORM.transaction_type == TransactionType.INITIAL_DEPOSIT.value
            )
            existing = self.session.scalars(stmt).first()
            if existing is not None and existing.id != transaction.id:
                raise ValueError("Initial capital has already been deposited; duplicate initial deposit rejected")

        orm = CapitalTransactionORM(
            id=transaction.id,
            experiment_id=transaction.experiment_id,
            transaction_type=transaction.transaction_type.value,
            amount=transaction.amount,
            description=transaction.description,
            recorded_at=transaction.recorded_at,
        )
        self.session.add(orm)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

        return self._to_pydantic(orm)

    def get_transaction(self, transaction_id: UUID) -> CapitalTransaction | None:
        """Retrieve a specific transaction by ID."""
        orm = self.session.get(CapitalTransactionORM, transaction_id)
        return self._to_pydantic(orm) if orm is not None else None

    def get_transaction_history(
        self,
        experiment_id: UUID | None = None,
        transaction_type: TransactionType | str | None = None,
    ) -> list[CapitalTransaction]:
        """Return full transaction history in chronological order.
        
        Optionally filter by experiment_id or transaction_type.
        """
        stmt = select(CapitalTransactionORM).order_by(
            CapitalTransactionORM.recorded_at.asc(),
            CapitalTransactionORM.id.asc(),
        )
        if experiment_id is not None:
            stmt = stmt.where(CapitalTransactionORM.experiment_id == experiment_id)
        if transaction_type is not None:
            type_val = (
                transaction_type.value
                if isinstance(transaction_type, TransactionType)
                else str(transaction_type)
            )
            stmt = stmt.where(CapitalTransactionORM.transaction_type == type_val)

        records = self.session.scalars(stmt).all()
        return [self._to_pydantic(r) for r in records]

    def get_financial_summary(self) -> FinancialSummary:
        """Calculate financial summary directly from the ledger transactions."""
        history = self.get_transaction_history()

        starting_capital = Decimal("0.00")
        total_revenue = Decimal("0.00")
        total_cost = Decimal("0.00")
        total_inflow = Decimal("0.00")
        total_outflow = Decimal("0.00")

        for tx in history:
            amount = tx.amount
            if tx.transaction_type == TransactionType.INITIAL_DEPOSIT:
                starting_capital += amount
                total_inflow += amount
            elif tx.transaction_type == TransactionType.REVENUE:
                total_revenue += amount
                total_inflow += amount
            elif tx.transaction_type == TransactionType.EXPERIMENT_REFUND:
                total_inflow += amount
            elif tx.transaction_type == TransactionType.EXPERIMENT_SPEND:
                total_cost += amount
                total_outflow += amount
            elif tx.transaction_type == TransactionType.WITHDRAWAL:
                total_outflow += amount
            elif tx.transaction_type == TransactionType.EXPERIMENT_ALLOCATION:
                # Budget reservation - not a direct cash outflow
                pass
            elif tx.transaction_type == TransactionType.ADJUSTMENT:
                # ponytail: adjustment direction deferred; neutral by default
                pass

        current_balance = total_inflow - total_outflow
        net_profit = total_revenue - total_cost

        roi: float | None = None
        if total_cost > Decimal("0.00"):
            roi = round(float(net_profit / total_cost), 4)

        total_allocated = self.get_total_active_allocations()
        available_unallocated = max(Decimal("0.00"), current_balance - total_allocated)

        return FinancialSummary(
            starting_capital=starting_capital,
            current_balance=current_balance,
            total_inflow=total_inflow,
            total_outflow=total_outflow,
            total_revenue=total_revenue,
            total_cost=total_cost,
            net_profit=net_profit,
            total_allocated=total_allocated,
            available_unallocated=available_unallocated,
            roi=roi,
        )

    def get_current_balance(self) -> Decimal:
        """Return current available capital in the pool."""
        history = self.get_transaction_history()
        total_inflow = Decimal("0.00")
        total_outflow = Decimal("0.00")
        for tx in history:
            amount = tx.amount
            if tx.transaction_type in (
                TransactionType.INITIAL_DEPOSIT,
                TransactionType.REVENUE,
                TransactionType.EXPERIMENT_REFUND,
            ):
                total_inflow += amount
            elif tx.transaction_type in (
                TransactionType.EXPERIMENT_SPEND,
                TransactionType.WITHDRAWAL,
            ):
                total_outflow += amount
        return total_inflow - total_outflow

    def get_experiment_actual_spend(self, experiment_id: UUID) -> Decimal:
        """Calculate total actual spend for an experiment directly from the capital ledger."""
        stmt = select(func.coalesce(func.sum(CapitalTransactionORM.amount), Decimal("0.00"))).where(
            CapitalTransactionORM.experiment_id == experiment_id,
            CapitalTransactionORM.transaction_type == TransactionType.EXPERIMENT_SPEND.value,
        )
        result = self.session.scalar(stmt)
        if result is None:
            return Decimal("0.00")
        return result if isinstance(result, Decimal) else Decimal(str(result))

    def get_total_active_allocations(self) -> Decimal:
        """Sum committed allocation remaining for active (approved or running) experiments."""
        stmt = select(ExperimentORM).where(
            ExperimentORM.status.in_([ExperimentStatus.APPROVED.value, ExperimentStatus.RUNNING.value])
        )
        active_experiments = self.session.scalars(stmt).all()
        total_allocated = Decimal("0.00")
        for exp in active_experiments:
            actual_spend = self.get_experiment_actual_spend(exp.id)
            allocated = (
                exp.allocated_budget
                if isinstance(exp.allocated_budget, Decimal)
                else Decimal(str(exp.allocated_budget))
            )
            remaining_allocated = max(Decimal("0.00"), allocated - actual_spend)
            total_allocated += remaining_allocated
        return total_allocated

    def get_available_unallocated_capital(self) -> Decimal:
        """Return liquid capital available for new experiment allocations (current_balance - active_allocations)."""
        return max(Decimal("0.00"), self.get_current_balance() - self.get_total_active_allocations())

    @staticmethod
    def _to_pydantic(orm: CapitalTransactionORM) -> CapitalTransaction:
        """Convert SQLAlchemy ORM row to canonical Pydantic model."""
        rec_at = orm.recorded_at
        if rec_at is not None and rec_at.tzinfo is None:
            rec_at = rec_at.replace(tzinfo=timezone.utc)

        return CapitalTransaction(
            id=orm.id,
            experiment_id=orm.experiment_id,
            transaction_type=TransactionType(orm.transaction_type),
            amount=orm.amount if isinstance(orm.amount, Decimal) else Decimal(str(orm.amount)),
            description=orm.description,
            recorded_at=rec_at,
        )
