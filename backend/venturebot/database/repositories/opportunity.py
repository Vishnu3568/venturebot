"""Repository for Opportunity persistence and queries."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from venturebot.database.models import OpportunityORM
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


class OpportunityRepository:
    """Repository managing Opportunity persistence in the SQLite database."""

    def __init__(self, session: Session, auto_commit: bool = True):
        self.session = session
        self.auto_commit = auto_commit

    def create(self, opportunity: Opportunity) -> Opportunity:
        """Persist a new Opportunity."""
        orm = OpportunityORM(
            id=opportunity.id,
            title=opportunity.title,
            description=opportunity.description,
            category=opportunity.category.value,
            status=opportunity.status.value,
            source=opportunity.source,
            evidence_notes=opportunity.evidence_notes,
            audience=opportunity.audience,
            trend_strength=opportunity.trend_strength,
            growth_indicators=opportunity.growth_indicators,
            competition_level=opportunity.competition_level,
            monetization_notes=opportunity.monetization_notes,
            production_difficulty=opportunity.production_difficulty,
            distribution_difficulty=opportunity.distribution_difficulty,
            automation_potential=opportunity.automation_potential,
            platform_dependency=opportunity.platform_dependency,
            regulatory_notes=opportunity.regulatory_notes,
            estimated_revenue_min=opportunity.estimated_revenue_min,
            estimated_revenue_max=opportunity.estimated_revenue_max,
            estimated_cost_min=opportunity.estimated_cost_min,
            estimated_cost_max=opportunity.estimated_cost_max,
            confidence=opportunity.confidence,
            created_at=opportunity.created_at,
        )
        self.session.add(orm)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

        return self._to_pydantic(orm)

    def get(self, opportunity_id: UUID) -> Opportunity | None:
        """Retrieve an Opportunity by unique ID."""
        orm = self.session.get(OpportunityORM, opportunity_id)
        return self._to_pydantic(orm) if orm is not None else None

    def list(
        self,
        status: OpportunityStatus | str | None = None,
        category: OpportunityCategory | str | None = None,
    ) -> list[Opportunity]:
        """List opportunities with optional filtering by status and category."""
        stmt = select(OpportunityORM).order_by(
            OpportunityORM.created_at.desc(),
            OpportunityORM.id.asc(),
        )
        if status is not None:
            status_val = status.value if isinstance(status, OpportunityStatus) else str(status)
            stmt = stmt.where(OpportunityORM.status == status_val)
        if category is not None:
            cat_val = category.value if isinstance(category, OpportunityCategory) else str(category)
            stmt = stmt.where(OpportunityORM.category == cat_val)

        records = self.session.scalars(stmt).all()
        return [self._to_pydantic(r) for r in records]

    def update_status(self, opportunity_id: UUID, status: OpportunityStatus | str) -> Opportunity | None:
        """Update the status of an existing Opportunity."""
        orm = self.session.get(OpportunityORM, opportunity_id)
        if orm is None:
            return None

        status_val = status.value if isinstance(status, OpportunityStatus) else str(status)
        orm.status = status_val
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()

        return self._to_pydantic(orm)

    @staticmethod
    def _to_pydantic(orm: OpportunityORM) -> Opportunity:
        """Map OpportunityORM to canonical Pydantic model."""
        created_at = orm.created_at
        if created_at is not None and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        return Opportunity(
            id=orm.id,
            title=orm.title,
            description=orm.description,
            category=OpportunityCategory(orm.category),
            status=OpportunityStatus(orm.status),
            source=orm.source or "",
            evidence_notes=orm.evidence_notes or "",
            audience=orm.audience or "",
            trend_strength=orm.trend_strength or "",
            growth_indicators=orm.growth_indicators or "",
            competition_level=orm.competition_level or "",
            monetization_notes=orm.monetization_notes or "",
            production_difficulty=orm.production_difficulty or "",
            distribution_difficulty=orm.distribution_difficulty or "",
            automation_potential=orm.automation_potential or "",
            platform_dependency=orm.platform_dependency or "",
            regulatory_notes=orm.regulatory_notes or "",
            estimated_revenue_min=(
                orm.estimated_revenue_min
                if isinstance(orm.estimated_revenue_min, Decimal)
                else Decimal(str(orm.estimated_revenue_min))
            ),
            estimated_revenue_max=(
                orm.estimated_revenue_max
                if isinstance(orm.estimated_revenue_max, Decimal)
                else Decimal(str(orm.estimated_revenue_max))
            ),
            estimated_cost_min=(
                orm.estimated_cost_min
                if isinstance(orm.estimated_cost_min, Decimal)
                else Decimal(str(orm.estimated_cost_min))
            ),
            estimated_cost_max=(
                orm.estimated_cost_max
                if isinstance(orm.estimated_cost_max, Decimal)
                else Decimal(str(orm.estimated_cost_max))
            ),
            confidence=orm.confidence,
            created_at=created_at,
        )
