"""Repository for ExternalExecution persistence and state tracking (Step 39)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from venturebot.database.models import ExternalExecutionORM
from venturebot.models.external_execution import ExternalExecution, ExternalExecutionStatus


class ExternalExecutionRepository:
    """Repository managing ExternalExecution persistence in the database."""

    def __init__(self, session: Session, auto_commit: bool = True):
        self.session = session
        self.auto_commit = auto_commit

    def get(self, execution_id: UUID) -> ExternalExecution | None:
        """Retrieve external execution record by ID."""
        orm = self.session.get(ExternalExecutionORM, execution_id)
        return self._to_pydantic(orm) if orm is not None else None

    def get_by_experiment_id(self, experiment_id: UUID) -> ExternalExecution | None:
        """Retrieve the current external execution record for an experiment."""
        stmt = select(ExternalExecutionORM).where(ExternalExecutionORM.experiment_id == experiment_id)
        orm = self.session.scalars(stmt).first()
        return self._to_pydantic(orm) if orm is not None else None

    def create(self, execution: ExternalExecution) -> ExternalExecution:
        """Persist a new ExternalExecution record."""
        orm = ExternalExecutionORM(
            id=execution.id,
            experiment_id=execution.experiment_id,
            channel=execution.channel,
            external_account_id=execution.external_account_id,
            campaign_id=execution.campaign_id,
            adset_id=execution.adset_id,
            creative_id=execution.creative_id,
            ad_id=execution.ad_id,
            image_hash=execution.image_hash,
            status=execution.status.value,
            last_error=execution.last_error,
            dispatched_at=execution.dispatched_at,
            updated_at=execution.updated_at,
        )
        self.session.add(orm)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()
        return self._to_pydantic(orm)

    def save(self, execution: ExternalExecution) -> ExternalExecution:
        """Update an existing ExternalExecution record with modified fields."""
        orm = self.session.get(ExternalExecutionORM, execution.id)
        if orm is None:
            raise ValueError(f"ExternalExecution '{execution.id}' does not exist.")

        orm.channel = execution.channel
        orm.external_account_id = execution.external_account_id
        orm.campaign_id = execution.campaign_id
        orm.adset_id = execution.adset_id
        orm.creative_id = execution.creative_id
        orm.ad_id = execution.ad_id
        orm.image_hash = execution.image_hash
        orm.status = execution.status.value
        orm.last_error = execution.last_error
        orm.updated_at = datetime.now(timezone.utc)

        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()
        return self._to_pydantic(orm)

    def update_status(
        self,
        execution_id: UUID,
        status: ExternalExecutionStatus,
        last_error: str | None = None,
    ) -> ExternalExecution:
        """Update status and optional last_error for an execution record."""
        orm = self.session.get(ExternalExecutionORM, execution_id)
        if orm is None:
            raise ValueError(f"ExternalExecution '{execution_id}' does not exist.")

        orm.status = status.value
        if last_error is not None:
            orm.last_error = last_error
        orm.updated_at = datetime.now(timezone.utc)

        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()
        return self._to_pydantic(orm)

    @staticmethod
    def _to_pydantic(orm: ExternalExecutionORM) -> ExternalExecution:
        disp_at = orm.dispatched_at
        if disp_at is not None and disp_at.tzinfo is None:
            disp_at = disp_at.replace(tzinfo=timezone.utc)

        upd_at = orm.updated_at
        if upd_at is not None and upd_at.tzinfo is None:
            upd_at = upd_at.replace(tzinfo=timezone.utc)

        return ExternalExecution(
            id=orm.id,
            experiment_id=orm.experiment_id,
            channel=orm.channel,
            external_account_id=orm.external_account_id,
            campaign_id=orm.campaign_id,
            adset_id=orm.adset_id,
            creative_id=orm.creative_id,
            ad_id=orm.ad_id,
            image_hash=orm.image_hash,
            status=ExternalExecutionStatus(orm.status),
            last_error=orm.last_error,
            dispatched_at=disp_at,
            updated_at=upd_at,
        )
