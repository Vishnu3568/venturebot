"""Research Collection Service for VentureBot (Step 20).

Provides a deterministic, controlled boundary for collecting external research
observations from concrete source adapters without interpreting business opportunities
or triggering persistence or financial side effects.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from venturebot.models.evidence import EvidenceCategory
from venturebot.opportunity.models import ResearchCollectionResult
from venturebot.opportunity.wikimedia import WikimediaPageviewsAdapter


class ResearchCollectionService:
    """Service governing controlled research collection runs from external sources."""

    SOURCE_WIKIMEDIA = "wikimedia_pageviews"

    @classmethod
    def collect_from_wikimedia(
        cls,
        target_date: date,
        limit: int | None = None,
        project: str = WikimediaPageviewsAdapter.DEFAULT_PROJECT,
        access: str = WikimediaPageviewsAdapter.DEFAULT_ACCESS,
        user_agent: str = WikimediaPageviewsAdapter.DEFAULT_USER_AGENT,
        timeout: float = 10.0,
        adapter_cls: type[WikimediaPageviewsAdapter] | Any = WikimediaPageviewsAdapter,
    ) -> ResearchCollectionResult:
        """Collect top pageviews research for an explicit date using the Wikimedia adapter.

        Enforces:
        - Explicit target date required (no default or implicit 'today').
        - Validates target date is a datetime.date instance.
        - Invokes WikimediaPageviewsAdapter.fetch_top_pageviews without duplicating logic.
        - Preserves all ResearchEvidenceItem items strictly as EvidenceCategory.FACT.
        - Returns a transient ResearchCollectionResult without database writes.
        - Zero financial side effects, zero Opportunity creation, zero autonomous discovery.
        """
        if not isinstance(target_date, date):
            raise ValueError("target_date must be a datetime.date instance.")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero when specified.")

        evidence_items = adapter_cls.fetch_top_pageviews(
            target_date=target_date,
            project=project,
            access=access,
            limit=limit,
            user_agent=user_agent,
            timeout=timeout,
        )

        # Invariant check: all collected items must remain FACT observations
        for item in evidence_items:
            if item.category != EvidenceCategory.FACT:
                raise ValueError(
                    f"External research observation must be classified as FACT, got {item.category}."
                )

        return ResearchCollectionResult(
            source_id=cls.SOURCE_WIKIMEDIA,
            collection_date=target_date,
            evidence_items=evidence_items,
        )
