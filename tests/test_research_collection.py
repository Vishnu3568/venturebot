"""Tests for Step 20 — Controlled Research Collection Boundary.

Verifies:
1. Explicit date is passed correctly to the Wikimedia adapter.
2. Explicit observation limit is passed and respected.
3. Returned evidence items are preserved in ResearchCollectionResult.
4. Evidence items strictly remain EvidenceCategory.FACT.
5. Source references remain intact and traceable.
6. Zero database writes occur during research collection.
7. Zero capital ledger transactions occur.
8. Zero Opportunities are automatically created by the collection boundary.
9. Adapter errors (HTTP, network, timeout, validation) propagate deterministically.
10. Empty evidence observations are handled deterministically.
11. ResearchCollectionResult contract enforces non-empty source_id and accurate count.
12. Integration with existing OpportunityIngestionService occurs only through explicit caller invocation.
"""

from collections.abc import Generator
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.learning import LearningRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus
from venturebot.opportunity.collection import ResearchCollectionService
from venturebot.opportunity.ingestion import OpportunityIngestionService
from venturebot.opportunity.models import (
    OpportunityIngestionPayload,
    ResearchCollectionResult,
    ResearchEvidenceItem,
)
from venturebot.opportunity.wikimedia import WikimediaPageviewsAdapter


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite in-memory database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


def make_sample_evidence_items(count: int = 3) -> list[ResearchEvidenceItem]:
    """Helper to produce standard FACT research evidence items."""
    items = []
    for i in range(1, count + 1):
        items.append(
            ResearchEvidenceItem(
                statement=f"Wikipedia article 'Topic_{i}' recorded {1000 * i} pageviews (rank #{i}) on 2026-09-16.",
                category=EvidenceCategory.FACT,
                source_reference=f"https://en.wikipedia.org/wiki/Topic_{i} (via https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/2026/09/16)",
            )
        )
    return items


# ── Parameter Handling & Invocation Tests ──────────────────────────────────────

def test_explicit_date_passed_to_adapter():
    """Explicit date is passed to the underlying Wikimedia adapter without guessing 'today'."""
    target = date(2026, 9, 16)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = make_sample_evidence_items(2)

    result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )

    assert isinstance(result, ResearchCollectionResult)
    assert result.collection_date == target
    assert result.source_id == ResearchCollectionService.SOURCE_WIKIMEDIA
    assert result.count == 2
    mock_adapter.fetch_top_pageviews.assert_called_once()
    assert mock_adapter.fetch_top_pageviews.call_args[1]["target_date"] == target


def test_rejects_non_date_parameter():
    """Rejects string or missing date parameter."""
    with pytest.raises(ValueError, match="target_date must be a datetime.date instance"):
        ResearchCollectionService.collect_from_wikimedia(target_date="2026-09-16")  # type: ignore


def test_observation_limit_respected():
    """Explicit observation limit is passed to the adapter and non-positive limit is rejected."""
    target = date(2026, 9, 16)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = make_sample_evidence_items(2)

    result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        limit=2,
        adapter_cls=mock_adapter,
    )

    assert result.count == 2
    assert mock_adapter.fetch_top_pageviews.call_args[1]["limit"] == 2

    with pytest.raises(ValueError, match="limit must be greater than zero"):
        ResearchCollectionService.collect_from_wikimedia(
            target_date=target,
            limit=0,
            adapter_cls=mock_adapter,
        )


# ── Evidence Preservation & Classification Tests ───────────────────────────────

def test_returned_evidence_items_preserved():
    """All items returned from the adapter are preserved in ResearchCollectionResult."""
    target = date(2026, 9, 16)
    sample_items = make_sample_evidence_items(3)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = sample_items

    result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )

    assert result.evidence_items == sample_items
    assert result.count == 3
    assert result.evidence_items[0].statement == sample_items[0].statement


def test_evidence_remains_fact_and_rejects_non_fact_invariant():
    """Evidence category must strictly remain FACT without conversion to inferences."""
    target = date(2026, 9, 16)
    sample_items = make_sample_evidence_items(2)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = sample_items

    result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )

    for item in result.evidence_items:
        assert item.category == EvidenceCategory.FACT

    # Invariant guard: if adapter returns non-FACT evidence, service rejects it
    non_fact_item = ResearchEvidenceItem(
        statement="Topic is very promising",
        category=EvidenceCategory.INFERENCE,
        source_reference="https://example.com",
    )
    mock_adapter.fetch_top_pageviews.return_value = [non_fact_item]

    with pytest.raises(ValueError, match="must be classified as FACT"):
        ResearchCollectionService.collect_from_wikimedia(
            target_date=target,
            adapter_cls=mock_adapter,
        )


def test_source_references_remain_intact():
    """Source references retain full URL traceability to canonical Wikipedia articles and endpoints."""
    target = date(2026, 9, 16)
    sample_items = make_sample_evidence_items(1)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = sample_items

    result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )

    ref = result.evidence_items[0].source_reference
    assert "https://en.wikipedia.org/wiki/Topic_1" in ref
    assert "https://wikimedia.org/api/rest_v1/metrics/pageviews/top" in ref


# ── Error & Empty State Handling Tests ─────────────────────────────────────────

def test_empty_evidence_result_handled_deterministically():
    """When adapter returns zero observations, an empty collection result is returned."""
    target = date(2026, 9, 16)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = []

    result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )

    assert isinstance(result, ResearchCollectionResult)
    assert result.evidence_items == []
    assert result.count == 0
    assert result.collection_date == target
    assert result.source_id == ResearchCollectionService.SOURCE_WIKIMEDIA


def test_adapter_errors_propagate_deterministically():
    """Underlying adapter HTTP and network errors propagate transparently."""
    target = date(2026, 9, 16)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.side_effect = RuntimeError("Wikimedia API HTTP 500 error: Internal Server Error")

    with pytest.raises(RuntimeError, match="Wikimedia API HTTP 500 error"):
        ResearchCollectionService.collect_from_wikimedia(
            target_date=target,
            adapter_cls=mock_adapter,
        )


def test_collection_result_contract_validation():
    """ResearchCollectionResult enforces valid non-empty source_id."""
    target = date(2026, 9, 16)
    with pytest.raises(ValueError, match="source_id must not be empty"):
        ResearchCollectionResult(source_id="   ", collection_date=target)


# ── Isolation & Opportunity Boundary Tests ─────────────────────────────────────

def test_no_database_write_occurs_during_collection(session: Session):
    """Collecting research causes zero database writes across all tables."""
    target = date(2026, 9, 16)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = make_sample_evidence_items(3)

    result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )

    assert result.count == 3

    # Assert zero records in all persistent tables
    assert OpportunityRepository(session).list() == []
    assert CapitalRepository(session).get_transaction_history() == []
    assert ExperimentRepository(session).list() == []
    assert DecisionRepository(session).list() == []
    assert LearningRepository(session).list_for_opportunity(uuid4()) == []


def test_no_ledger_transaction_occurs(session: Session):
    """Collecting research causes zero financial side effects."""
    target = date(2026, 9, 16)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = make_sample_evidence_items(3)

    ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )

    cap_repo = CapitalRepository(session)
    assert cap_repo.get_transaction_history() == []
    assert cap_repo.get_current_balance() == Decimal("0.00")


def test_no_opportunity_automatically_created():
    """ResearchCollectionService returns strictly ResearchCollectionResult, never an Opportunity."""
    target = date(2026, 9, 16)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = make_sample_evidence_items(3)

    result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )

    assert isinstance(result, ResearchCollectionResult)
    assert not isinstance(result, Opportunity)
    for item in result.evidence_items:
        assert not isinstance(item, Opportunity)


def test_integration_collection_to_explicit_opportunity_ingestion(session: Session):
    """End-to-end integration: Research collection feeds OpportunityIngestionService ONLY upon caller invocation."""
    target = date(2026, 9, 16)
    mock_adapter = MagicMock(spec=WikimediaPageviewsAdapter)
    mock_adapter.fetch_top_pageviews.return_value = make_sample_evidence_items(2)

    # 1. Collect research observations
    collection_result = ResearchCollectionService.collect_from_wikimedia(
        target_date=target,
        adapter_cls=mock_adapter,
    )
    assert collection_result.count == 2

    # Database still completely empty
    opp_repo = OpportunityRepository(session)
    assert opp_repo.list() == []

    # 2. Caller constructs explicit OpportunityIngestionPayload using collected evidence
    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="Topic 1 Niche Guide",
            description="Deep dive educational guide on Topic 1 following surging attention.",
            category=OpportunityCategory.CONTENT,
            status=OpportunityStatus.UNDER_REVIEW,  # Must be forced to DISCOVERED
            audience="Topic 1 enthusiasts",
            monetization_notes="Digital guide and affiliate resources",
            estimated_revenue_min=Decimal("200.00"),
            estimated_revenue_max=Decimal("800.00"),
            estimated_cost_min=Decimal("20.00"),
            estimated_cost_max=Decimal("100.00"),
        ),
        evidence_items=collection_result.evidence_items,
    )

    # 3. Explicit ingestion into the database
    persisted_opp = OpportunityIngestionService.ingest(session, payload)

    # 4. Ingested opportunity invariants
    assert persisted_opp.status == OpportunityStatus.DISCOVERED
    assert "[FACT] Wikipedia article 'Topic_1'" in persisted_opp.evidence_notes
    assert "[FACT] Wikipedia article 'Topic_2'" in persisted_opp.evidence_notes
    assert "https://en.wikipedia.org/wiki/Topic_1" in persisted_opp.source
    assert "https://en.wikipedia.org/wiki/Topic_2" in persisted_opp.source

    # 5. Exactly one Opportunity persisted
    all_opps = opp_repo.list()
    assert len(all_opps) == 1
    assert all_opps[0].id == persisted_opp.id

    # 6. Financial ledger remains untouched
    assert CapitalRepository(session).get_transaction_history() == []
