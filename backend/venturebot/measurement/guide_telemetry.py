"""Guide Access Telemetry Ingestion Contract & Service (Step 72).

Defines the authoritative contract between external edge telemetry (e.g. Cloudflare)
and VentureBot's ExperimentMetrics system for GUIDE_ACCESS observations.

Enforces:
- Observational FACT evidence classification (Section 17).
- Canonical source_reference: edge:telemetry:guide_access:<experiment_id>:<date_start>:<date_stop>
- Idempotency & Restatement contract (Section 24):
  * Exact duplicate (same count for same window) -> DUPLICATE_NO_OP
  * Restatement (updated count for same window) -> RESTATEMENT_APPENDED
  * First observation -> INGESTED
- Strict semantic boundary:
  * guide_accesses = server-observed guide asset access/request count
  * visitors = None (requests != unique human visitors)
  * conversions = None (access != commercial conversion)
  * revenue/profit/roi/roas = None (unmonetized informational pilot)
  * cost = None (telemetry does not fabricate an unauthoritative cost observation)
- Absolute financial ledger isolation (zero CapitalTransaction mutations).
- Zero automated decisions (no ExperimentDecision mutations).
- Zero experiment lifecycle transitions (remains DRAFT).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentORM
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.measurement.service import ExperimentMeasurementService
from venturebot.measurement.telemetry import TelemetryIngestionStatus
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.metrics import ExperimentMetrics


def build_canonical_guide_access_source_reference(
    experiment_id: UUID | str,
    date_start: date,
    date_stop: date,
) -> str:
    """Build the canonical logical reporting window identity string for guide access telemetry."""
    clean_id = str(experiment_id).strip()
    return f"edge:telemetry:guide_access:{clean_id}:{date_start.isoformat()}:{date_stop.isoformat()}"


class GuideAccessTelemetrySummary(BaseModel):
    """Verified contract payload returned by the edge telemetry retrieval endpoint.

    Corresponds to GET /api/v1/telemetry/summary.
    """

    experiment_id: UUID = Field(description="VentureBot experiment ID")
    event_type: str = Field(default="guide_access", description="Event type; must be 'guide_access'")
    count: int = Field(ge=0, description="Authoritative aggregate guide access count")
    date_start: date = Field(description="Observation window start date (YYYY-MM-DD)")
    date_stop: date = Field(description="Observation window stop date (YYYY-MM-DD)")
    source_reference: str = Field(default="", description="Canonical reporting-window identity")
    evidence_type: EvidenceCategory = Field(default=EvidenceCategory.FACT, description="Evidence category")
    updated_at: datetime | None = Field(default=None, description="Authoritative edge update timestamp")

    @model_validator(mode="after")
    def validate_contract(self) -> "GuideAccessTelemetrySummary":
        if self.event_type != "guide_access":
            raise ValueError(f"Unsupported event_type '{self.event_type}'. Only 'guide_access' is accepted.")
        if self.date_stop < self.date_start:
            raise ValueError(f"date_stop ({self.date_stop}) cannot be earlier than date_start ({self.date_start}).")
        if not self.source_reference:
            self.source_reference = build_canonical_guide_access_source_reference(
                self.experiment_id, self.date_start, self.date_stop
            )
        return self


class GuideAccessTelemetryIngestionResult(BaseModel):
    """Result contract for guide access telemetry ingestion."""

    status: TelemetryIngestionStatus = Field(description="Ingestion outcome status")
    experiment_id: UUID = Field(description="VentureBot experiment ID")
    date_start: date = Field(description="Observation window start date")
    date_stop: date = Field(description="Observation window stop date")
    source_reference: str = Field(description="Canonical logical reporting window identity")
    metrics: ExperimentMetrics = Field(description="Authoritative ExperimentMetrics record for this ingestion")
    message: str = Field(description="Human-readable result summary")


class GuideAccessTelemetryIngestionService:
    """Service governing on-demand ingestion of edge guide access telemetry into ExperimentMetrics."""

    @classmethod
    def ingest_summary(
        cls,
        session: Session,
        summary: GuideAccessTelemetrySummary,
        auto_commit: bool = True,
    ) -> GuideAccessTelemetryIngestionResult:
        """Ingest an authoritative guide access summary observation into ExperimentMetrics.

        Enforces:
        1. Experiment must exist in database.
        2. Event type must be strictly 'guide_access'.
        3. date_stop >= date_start.
        4. Canonical source_reference formulation.
        5. Idempotency & restatement resolution:
           - Same values -> DUPLICATE_NO_OP (idempotent no-op).
           - Differing values -> RESTATEMENT_APPENDED (new immutable FACT record).
           - First observation -> INGESTED (new immutable FACT record).
        6. Zero financial transactions, zero automated decisions, zero status transitions.
        """
        # 1. Experiment must exist
        exp = session.get(ExperimentORM, summary.experiment_id)
        if exp is None:
            raise ValueError(f"Experiment '{summary.experiment_id}' does not exist.")

        # 2. Validate event type
        if summary.event_type != "guide_access":
            raise ValueError(f"Unsupported event_type '{summary.event_type}'. Only 'guide_access' is supported.")

        # 3. Validate dates
        if summary.date_stop < summary.date_start:
            raise ValueError(
                f"date_stop ({summary.date_stop.isoformat()}) cannot be earlier than date_start ({summary.date_start.isoformat()})."
            )

        # 4. Resolve canonical source reference
        source_ref = (
            summary.source_reference.strip()
            if summary.source_reference
            else build_canonical_guide_access_source_reference(
                summary.experiment_id, summary.date_start, summary.date_stop
            )
        )

        # 5. Query existing measurements for duplicate / restatement check
        metrics_repo = MetricsRepository(session, auto_commit=False)
        existing_measurements = metrics_repo.list_for_experiment(summary.experiment_id)
        matching_window = [m for m in existing_measurements if m.source_reference == source_ref]

        if matching_window:
            latest_existing = matching_window[-1]

            # Check for exact duplicate
            is_duplicate = latest_existing.guide_accesses == summary.count

            if is_duplicate:
                return GuideAccessTelemetryIngestionResult(
                    status=TelemetryIngestionStatus.DUPLICATE_NO_OP,
                    experiment_id=summary.experiment_id,
                    date_start=summary.date_start,
                    date_stop=summary.date_stop,
                    source_reference=source_ref,
                    metrics=latest_existing,
                    message="Identical guide access telemetry observation already recorded for this reporting window (idempotent no-op).",
                )

            # Restatement: count has updated for the same reporting window
            new_metrics = ExperimentMetrics(
                experiment_id=summary.experiment_id,
                evidence_type=EvidenceCategory.FACT,
                source_reference=source_ref,
                impressions=None,
                clicks=None,
                visitors=None,
                guide_accesses=summary.count,
                conversions=None,
                conversion_rate=None,
                cost=None,
                revenue=None,
                profit_loss=None,
                roas=None,
                roi=None,
                retention_notes="",
                recorded_at=datetime.now(timezone.utc),
            )
            persisted = ExperimentMeasurementService.record_measurement(
                session,
                new_metrics,
                auto_commit=auto_commit,
            )
            return GuideAccessTelemetryIngestionResult(
                status=TelemetryIngestionStatus.RESTATEMENT_APPENDED,
                experiment_id=summary.experiment_id,
                date_start=summary.date_start,
                date_stop=summary.date_stop,
                source_reference=source_ref,
                metrics=persisted,
                message="Restated guide access observation appended for existing reporting window.",
            )

        # First observation for this reporting window
        new_metrics = ExperimentMetrics(
            experiment_id=summary.experiment_id,
            evidence_type=EvidenceCategory.FACT,
            source_reference=source_ref,
            impressions=None,
            clicks=None,
            visitors=None,
            guide_accesses=summary.count,
            conversions=None,
            conversion_rate=None,
            cost=None,
            revenue=None,
            profit_loss=None,
            roas=None,
            roi=None,
            retention_notes="",
            recorded_at=datetime.now(timezone.utc),
        )
        persisted = ExperimentMeasurementService.record_measurement(
            session,
            new_metrics,
            auto_commit=auto_commit,
        )
        return GuideAccessTelemetryIngestionResult(
            status=TelemetryIngestionStatus.INGESTED,
            experiment_id=summary.experiment_id,
            date_start=summary.date_start,
            date_stop=summary.date_stop,
            source_reference=source_ref,
            metrics=persisted,
            message="Guide access telemetry observation successfully ingested.",
        )
