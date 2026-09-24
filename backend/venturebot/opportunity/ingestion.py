"""Opportunity Ingestion Service for VentureBot (Step 16).

Provides deterministic ingestion of discrete research evidence findings into
canonical Opportunity records with DISCOVERED status.
Enforces evidence validation without creating duplicate database entities.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.models.opportunity import Opportunity, OpportunityStatus
from venturebot.opportunity.models import (
    HumanOpportunitySpecification,
    OpportunityCandidate,
    OpportunityIngestionPayload,
)


class OpportunityIngestionService:
    """Service governing deterministic ingestion of research findings into Opportunities."""

    @classmethod
    def ingest(
        cls,
        session: Session,
        payload: OpportunityIngestionPayload,
        auto_commit: bool = True,
    ) -> Opportunity:
        """Ingest research evidence and create a canonical Opportunity in DISCOVERED status.

        Enforces:
        - Research evidence validation:
          - Each item has a non-empty statement and non-empty source_reference.
          - Categories (FACT, INFERENCE, HYPOTHESIS) are strictly preserved.
          - Never transforms INFERENCE -> FACT or HYPOTHESIS -> FACT.
        - Preserves evidence provenance into canonical Opportunity source and evidence_notes fields.
        - Created Opportunity is strictly in DISCOVERED status.
        - Exactly ONE domain write: creates the Opportunity via OpportunityRepository.
        - Zero capital transactions, zero decisions created, zero experiments created.
        """
        opp_data = payload.opportunity

        # 1. Validate and process research evidence items
        evidence_notes_lines: list[str] = []
        source_refs: list[str] = []

        for item in payload.evidence_items:
            clean_stmt = item.statement.strip() if item.statement else ""
            clean_src = item.source_reference.strip() if item.source_reference else ""
            if not clean_stmt:
                raise ValueError("Evidence statement must not be empty or whitespace.")
            if not clean_src:
                raise ValueError("Evidence source_reference must not be empty or whitespace.")

            # Deterministic formatting preserving category and provenance
            cat_label = item.category.value.upper()
            evidence_notes_lines.append(f"[{cat_label}] {clean_stmt} (Source: {clean_src})")
            if clean_src not in source_refs:
                source_refs.append(clean_src)

        # 2. Determine mapped source
        mapped_source = opp_data.source.strip() if opp_data.source else ""
        if source_refs:
            joined_sources = ", ".join(source_refs)
            if not mapped_source:
                mapped_source = joined_sources
            elif joined_sources not in mapped_source:
                mapped_source = f"{mapped_source}; {joined_sources}"

        # 3. Determine mapped evidence_notes
        mapped_notes = opp_data.evidence_notes.strip() if opp_data.evidence_notes else ""
        if evidence_notes_lines:
            compiled_notes = "\n".join(evidence_notes_lines)
            if not mapped_notes:
                mapped_notes = compiled_notes
            else:
                mapped_notes = f"{mapped_notes}\n{compiled_notes}"

        # 4. Construct validated canonical Opportunity with DISCOVERED status
        target_opportunity = Opportunity(
            id=opp_data.id,
            title=opp_data.title,
            description=opp_data.description,
            category=opp_data.category,
            status=OpportunityStatus.DISCOVERED,  # Enforce DISCOVERED
            source=mapped_source,
            evidence_notes=mapped_notes,
            audience=opp_data.audience,
            trend_strength=opp_data.trend_strength,
            growth_indicators=opp_data.growth_indicators,
            competition_level=opp_data.competition_level,
            monetization_notes=opp_data.monetization_notes,
            production_difficulty=opp_data.production_difficulty,
            distribution_difficulty=opp_data.distribution_difficulty,
            automation_potential=opp_data.automation_potential,
            platform_dependency=opp_data.platform_dependency,
            regulatory_notes=opp_data.regulatory_notes,
            estimated_revenue_min=opp_data.estimated_revenue_min,
            estimated_revenue_max=opp_data.estimated_revenue_max,
            estimated_cost_min=opp_data.estimated_cost_min,
            estimated_cost_max=opp_data.estimated_cost_max,
            confidence=opp_data.confidence,
            created_at=opp_data.created_at,
        )

        # 5. Persist through OpportunityRepository
        repo = OpportunityRepository(session, auto_commit=auto_commit)
        return repo.create(target_opportunity)

    @classmethod
    def ingest_specification(
        cls,
        session: Session,
        specification: HumanOpportunitySpecification,
        candidate: OpportunityCandidate | None = None,
        auto_commit: bool = True,
    ) -> Opportunity:
        """Ingest a human-reviewed opportunity specification into a canonical Opportunity."""
        payload = specification.to_ingestion_payload(candidate=candidate)
        return cls.ingest(session, payload, auto_commit=auto_commit)
