"""Repository for ExternalExecution persistence and state tracking (Step 39)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from venturebot.database.models import ExternalExecutionEventORM, ExternalExecutionORM
from venturebot.models.external_execution import (
    ExternalExecution,
    ExternalExecutionEvent,
    ExternalExecutionEventType,
    ExternalExecutionStatus,
)


class ExternalExecutionRepository:
    """Repository managing ExternalExecution persistence in the database."""

    def __init__(self, session: Session, auto_commit: bool = True):
        self.session = session
        self.auto_commit = auto_commit

    def get(self, execution_id: UUID) -> ExternalExecution | None:
        """Retrieve external execution record by ID."""
        orm = self.session.get(ExternalExecutionORM, execution_id)
        return self._to_pydantic(orm) if orm is not None else None

    def get_by_id(self, execution_id: UUID) -> ExternalExecution | None:
        """Retrieve external execution record by ID (alias for get)."""
        return self.get(execution_id)

    def get_by_experiment_id(self, experiment_id: UUID) -> ExternalExecution | None:
        """Retrieve the current external execution record for an experiment."""
        stmt = select(ExternalExecutionORM).where(ExternalExecutionORM.experiment_id == experiment_id)
        orm = self.session.scalars(stmt).first()
        return self._to_pydantic(orm) if orm is not None else None

    def get_by_campaign_id(self, campaign_id: str) -> list[ExternalExecution]:
        """Retrieve all external execution records referencing the exact campaign ID."""
        clean_id = campaign_id.strip() if campaign_id else ""
        if not clean_id:
            return []
        stmt = select(ExternalExecutionORM).where(ExternalExecutionORM.campaign_id == clean_id)
        orms = self.session.scalars(stmt).all()
        return [self._to_pydantic(o) for o in orms]

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

    def record_event(self, event: ExternalExecutionEvent) -> ExternalExecutionEvent:
        """Persist an immutable audit log event for an external execution."""
        orm = ExternalExecutionEventORM(
            id=event.id,
            execution_id=event.execution_id,
            event_type=event.event_type,
            tier=event.tier,
            old_resource_id=event.old_resource_id,
            new_resource_id=event.new_resource_id,
            reason=event.reason,
            api_request_summary=event.api_request_summary,
            api_response_summary=event.api_response_summary,
            created_at=event.created_at,
        )
        self.session.add(orm)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()
        return self._event_to_pydantic(orm)

    def get_events(self, execution_id: UUID) -> list[ExternalExecutionEvent]:
        """Retrieve all audit events for an execution in chronological order."""
        stmt = (
            select(ExternalExecutionEventORM)
            .where(ExternalExecutionEventORM.execution_id == execution_id)
            .order_by(ExternalExecutionEventORM.created_at.asc())
        )
        orms = self.session.scalars(stmt).all()
        return [self._event_to_pydantic(o) for o in orms]

    def get_superseded_resources(
        self, execution_id: UUID, tier: str | None = None
    ) -> list[ExternalExecutionEvent]:
        """Retrieve all resource superseding events for an execution."""
        stmt = (
            select(ExternalExecutionEventORM)
            .where(
                ExternalExecutionEventORM.execution_id == execution_id,
                ExternalExecutionEventORM.event_type == ExternalExecutionEventType.RESOURCE_SUPERSEDED.value,
            )
            .order_by(ExternalExecutionEventORM.created_at.asc())
        )
        if tier is not None:
            stmt = stmt.where(ExternalExecutionEventORM.tier == tier)
        orms = self.session.scalars(stmt).all()
        return [self._event_to_pydantic(o) for o in orms]

    def get_events_by_resource_id(
        self, resource_id: str, tier: str | None = None
    ) -> list[ExternalExecutionEvent]:
        """Retrieve all historical audit events mentioning a resource ID as old or new."""
        clean_id = resource_id.strip() if resource_id else ""
        if not clean_id:
            return []
        stmt = select(ExternalExecutionEventORM).where(
            (ExternalExecutionEventORM.new_resource_id == clean_id)
            | (ExternalExecutionEventORM.old_resource_id == clean_id)
        )
        if tier is not None:
            stmt = stmt.where(ExternalExecutionEventORM.tier == tier)
        stmt = stmt.order_by(ExternalExecutionEventORM.created_at.asc())
        orms = self.session.scalars(stmt).all()
        return [self._event_to_pydantic(o) for o in orms]

    @staticmethod
    def _event_to_pydantic(orm: ExternalExecutionEventORM) -> ExternalExecutionEvent:
        c_at = orm.created_at
        if c_at is not None and c_at.tzinfo is None:
            c_at = c_at.replace(tzinfo=timezone.utc)
        return ExternalExecutionEvent(
            id=orm.id,
            execution_id=orm.execution_id,
            event_type=orm.event_type,
            tier=orm.tier,
            old_resource_id=orm.old_resource_id,
            new_resource_id=orm.new_resource_id,
            reason=orm.reason,
            api_request_summary=orm.api_request_summary,
            api_response_summary=orm.api_response_summary,
            created_at=c_at,
        )

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
