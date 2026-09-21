"""Meta Insights Telemetry Ingestion Service (Step 44).

Provides an on-demand, read-only telemetry ingestion gateway from Meta Graph API
into VentureBot's ExperimentMetrics system.

Enforces:
- Scoped strictly to campaign-level Meta Insights.
- Strict Experiment -> ExternalExecution -> campaign_id mapping.
- Observational FACT evidence classification (Section 17).
- Canonical source_reference: meta:insights:campaign:<campaign_id>:<date_start>:<date_stop>
- Idempotency & Restatement contract (Section 24):
  * Exact duplicate (same values) -> DUPLICATE_NO_OP
  * Restatement (changed values) -> RESTATEMENT_APPENDED
  * First observation -> INGESTED
- Absolute financial ledger isolation (zero CapitalTransaction mutations).
- Zero automated decisions (no ExperimentDecision mutations).
- Zero experiment lifecycle transitions.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from venturebot.database.models import ExperimentORM
from venturebot.database.repositories.external_execution import ExternalExecutionRepository
from venturebot.database.repositories.metrics import MetricsRepository
from venturebot.execution.meta import MetaInsightsTelemetry, MetaMarketingApiAdapter
from venturebot.measurement.service import ExperimentMeasurementService
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.metrics import ExperimentMetrics


class TelemetryIngestionStatus(str, Enum):
    """Status outcome of an on-demand telemetry ingestion request."""

    INGESTED = "ingested"
    DUPLICATE_NO_OP = "duplicate_no_op"
    RESTATEMENT_APPENDED = "restatement_appended"


class MetaTelemetryIngestionResult(BaseModel):
    """Result contract for on-demand Meta Insights telemetry ingestion."""

    status: TelemetryIngestionStatus = Field(description="Ingestion outcome status")
    experiment_id: UUID = Field(description="VentureBot experiment ID")
    campaign_id: str = Field(description="Associated Meta Campaign ID")
    date_start: date = Field(description="Observation window start date")
    date_stop: date = Field(description="Observation window stop date")
    source_reference: str = Field(description="Canonical logical reporting window identity")
    metrics: ExperimentMetrics = Field(description="Authoritative ExperimentMetrics record for this ingestion")
    message: str = Field(description="Human-readable result summary")


class MetaTelemetryIngestionService:
    """Service governing on-demand ingestion of Meta Insights telemetry into ExperimentMetrics."""

    @classmethod
    def build_canonical_source_reference(
        cls,
        campaign_id: str,
        date_start: date,
        date_stop: date,
    ) -> str:
        """Build the canonical logical reporting window identity string per Section 24."""
        clean_id = campaign_id.strip() if campaign_id else ""
        return f"meta:insights:campaign:{clean_id}:{date_start.isoformat()}:{date_stop.isoformat()}"

    @classmethod
    def ingest_campaign_metrics(
        cls,
        session: Session,
        experiment_id: UUID,
        date_start: date | str,
        date_stop: date | str,
        adapter: MetaMarketingApiAdapter | None = None,
        auto_commit: bool = True,
    ) -> MetaTelemetryIngestionResult:
        """Ingest campaign-level Meta Insights telemetry for an experiment in an observation window.

        Enforces:
        1. Experiment must exist.
        2. ExternalExecution must exist for the experiment.
        3. Channel must be 'meta'.
        4. external_account_id and campaign_id must be present.
        5. date_start and date_stop must be valid and date_stop >= date_start.
        6. Calls read-only Meta adapter get_insights().
        7. Validates that exactly one campaign-level aggregate observation is returned.
        8. Verifies returned campaign_id matches the expected campaign_id.
        9. Checks existing records for that logical reporting window:
           - If identical values -> DUPLICATE_NO_OP (no-op, return existing record).
           - If differing values -> RESTATEMENT_APPENDED (append new immutable FACT record).
           - If first observation -> INGESTED (append new immutable FACT record).
        10. Zero financial ledger transactions, zero automated decisions, zero lifecycle mutations.
        """
        # 1. Experiment must exist
        exp = session.get(ExperimentORM, experiment_id)
        if exp is None:
            raise ValueError(f"Experiment '{experiment_id}' does not exist.")

        # 2. ExternalExecution must exist
        ext_repo = ExternalExecutionRepository(session, auto_commit=False)
        ext_exec = ext_repo.get_by_experiment_id(experiment_id)
        if ext_exec is None:
            raise ValueError(f"No ExternalExecution record found for experiment '{experiment_id}'.")

        # 3. Channel must be meta
        if not ext_exec.channel or ext_exec.channel.strip().lower() != "meta":
            raise ValueError(f"ExternalExecution channel '{ext_exec.channel}' is not 'meta'.")

        # 4. Account ID & Campaign ID must be known
        account_id = ext_exec.external_account_id.strip() if ext_exec.external_account_id else ""
        if not account_id:
            raise ValueError(f"ExternalExecution for experiment '{experiment_id}' is missing external_account_id.")

        campaign_id = ext_exec.campaign_id.strip() if ext_exec.campaign_id else ""
        if not campaign_id:
            raise ValueError(f"ExternalExecution for experiment '{experiment_id}' is missing campaign_id.")

        # 5. Parse & validate dates
        if isinstance(date_start, str):
            clean_start = date_start.strip()
            if not clean_start:
                raise ValueError("date_start must not be empty.")
            parsed_start = date.fromisoformat(clean_start)
        elif isinstance(date_start, date):
            parsed_start = date_start
        else:
            raise ValueError(f"Invalid date_start type: {type(date_start).__name__}. Expected date or YYYY-MM-DD string.")

        if isinstance(date_stop, str):
            clean_stop = date_stop.strip()
            if not clean_stop:
                raise ValueError("date_stop must not be empty.")
            parsed_stop = date.fromisoformat(clean_stop)
        elif isinstance(date_stop, date):
            parsed_stop = date_stop
        else:
            raise ValueError(f"Invalid date_stop type: {type(date_stop).__name__}. Expected date or YYYY-MM-DD string.")

        if parsed_stop < parsed_start:
            raise ValueError(
                f"date_stop ({parsed_stop.isoformat()}) cannot be earlier than date_start ({parsed_start.isoformat()})."
            )

        # 6. Resolve adapter & query Meta Insights
        api_adapter = (
            adapter
            if adapter is not None
            else MetaMarketingApiAdapter(ad_account_id=account_id)
        )

        insights_list = api_adapter.get_insights(
            object_id=campaign_id,
            time_range={
                "since": parsed_start.isoformat(),
                "until": parsed_stop.isoformat(),
            },
            level="campaign",
        )

        # 7. Validate response shape
        if not insights_list:
            raise ValueError(
                f"No Meta Insights telemetry data returned for campaign '{campaign_id}' "
                f"in window {parsed_start.isoformat()} to {parsed_stop.isoformat()}."
            )
        if len(insights_list) > 1:
            raise ValueError(
                f"Unexpected multiple Insights rows ({len(insights_list)}) returned for campaign '{campaign_id}'. "
                "Expected a single campaign-level aggregate."
            )

        telemetry: MetaInsightsTelemetry = insights_list[0]

        # Verify campaign_id matches if provided in telemetry
        if telemetry.campaign_id and telemetry.campaign_id != campaign_id:
            raise ValueError(
                f"Returned telemetry campaign_id '{telemetry.campaign_id}' does not match expected campaign '{campaign_id}'."
            )

        # 8. Formulate canonical source reference
        source_ref = cls.build_canonical_source_reference(campaign_id, parsed_start, parsed_stop)

        # 9. Query existing measurements for duplicate / restatement check
        metrics_repo = MetricsRepository(session, auto_commit=False)
        existing_measurements = metrics_repo.list_for_experiment(experiment_id)
        matching_window = [m for m in existing_measurements if m.source_reference == source_ref]

        if matching_window:
            # Latest observation for this logical reporting window
            latest_existing = matching_window[-1]

            # Check for exact duplicate
            is_duplicate = (
                latest_existing.cost == telemetry.spend
                and latest_existing.impressions == telemetry.impressions
                and latest_existing.clicks == telemetry.clicks
            )

            if is_duplicate:
                return MetaTelemetryIngestionResult(
                    status=TelemetryIngestionStatus.DUPLICATE_NO_OP,
                    experiment_id=experiment_id,
                    campaign_id=campaign_id,
                    date_start=parsed_start,
                    date_stop=parsed_stop,
                    source_reference=source_ref,
                    metrics=latest_existing,
                    message="Identical telemetry observation already recorded for this reporting window (idempotent no-op).",
                )

            # Restatement: values have changed
            new_metrics = ExperimentMetrics(
                experiment_id=experiment_id,
                evidence_type=EvidenceCategory.FACT,
                source_reference=source_ref,
                impressions=telemetry.impressions,
                clicks=telemetry.clicks,
                visitors=None,
                conversions=None,
                cost=telemetry.spend,
                revenue=None,
                recorded_at=datetime.now(timezone.utc),
            )
            persisted = ExperimentMeasurementService.record_measurement(
                session,
                new_metrics,
                auto_commit=auto_commit,
            )
            return MetaTelemetryIngestionResult(
                status=TelemetryIngestionStatus.RESTATEMENT_APPENDED,
                experiment_id=experiment_id,
                campaign_id=campaign_id,
                date_start=parsed_start,
                date_stop=parsed_stop,
                source_reference=source_ref,
                metrics=persisted,
                message="Restated telemetry observation appended for existing reporting window.",
            )

        # New initial observation for this window
        new_metrics = ExperimentMetrics(
            experiment_id=experiment_id,
            evidence_type=EvidenceCategory.FACT,
            source_reference=source_ref,
            impressions=telemetry.impressions,
            clicks=telemetry.clicks,
            visitors=None,
            conversions=None,
            cost=telemetry.spend,
            revenue=None,
            recorded_at=datetime.now(timezone.utc),
        )
        persisted = ExperimentMeasurementService.record_measurement(
            session,
            new_metrics,
            auto_commit=auto_commit,
        )
        return MetaTelemetryIngestionResult(
            status=TelemetryIngestionStatus.INGESTED,
            experiment_id=experiment_id,
            campaign_id=campaign_id,
            date_start=parsed_start,
            date_stop=parsed_stop,
            source_reference=source_ref,
            metrics=persisted,
            message="Campaign telemetry observation successfully ingested.",
        )
