"""Tests for Step 16 — Opportunity Ingestion & Research Evidence Foundation.

Verifies:
1. Valid ingestion creates an Opportunity.
2. Created Opportunity status is strictly DISCOVERED.
3. Valid research evidence is accepted and preserved into source and evidence_notes.
4. FACT evidence requires source_reference.
5. Empty/whitespace statement is rejected.
6. Empty/whitespace source_reference is rejected.
7. HYPOTHESIS remains HYPOTHESIS in the transient contract.
8. Existing Opportunity validation is respected.
9. Existing OpportunityRepository is used for persistence.
10. Ingestion does not create capital transactions.
11. Ingestion does not create experiments.
12. Ingestion does not create decisions.
13. Ingestion does not modify existing opportunities.
"""

from collections.abc import Generator
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.models.evidence import EvidenceCategory
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus
from venturebot.opportunity.ingestion import OpportunityIngestionService
from venturebot.opportunity.models import OpportunityIngestionPayload, ResearchEvidenceItem


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite in-memory database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


# ── Tests 1 to 3: Valid Ingestion & Status Enforcement ────────────────────────────

def test_valid_ingestion_creates_opportunity_with_discovered_status(session: Session):
    """Test 1 & 2: Ingestion persists an Opportunity strictly in DISCOVERED status."""
    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="Podcast Audio Enhancer CLI",
            description="CLI tool to remove background noise from indie podcast tracks.",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.APPROVED,  # Intentionally passed APPROVED to verify override
            audience="Indie podcasters",
            monetization_notes="₹499 lifetime license",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("1500.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
            confidence=0.8,
        ),
        evidence_items=[
            ResearchEvidenceItem(
                statement="8 podcast hosts confirmed audio cleaning takes > 2 hours per episode",
                category=EvidenceCategory.FACT,
                source_reference="Reddit r/podcasting survey thread #442",
            )
        ],
    )

    created_opp = OpportunityIngestionService.ingest(session, payload)

    assert created_opp.id is not None
    assert created_opp.title == "Podcast Audio Enhancer CLI"
    assert created_opp.status == OpportunityStatus.DISCOVERED  # Must be strictly DISCOVERED
    assert "Reddit r/podcasting survey thread #442" in created_opp.source
    assert "[FACT]" in created_opp.evidence_notes
    assert "8 podcast hosts confirmed" in created_opp.evidence_notes


def test_valid_research_evidence_accepted_and_formatted(session: Session):
    """Test 3: Multiple research evidence items are deterministically compiled."""
    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="Local Dog Walker Aggregator",
            description="WhatsApp directory for local pet services.",
            category=OpportunityCategory.SERVICE,
            source="Primary field survey",
            evidence_notes="Initial exploratory notes",
        ),
        evidence_items=[
            ResearchEvidenceItem(
                statement="Over 120 pet owners requested reliable dog walkers in Indiranagar group",
                category=EvidenceCategory.FACT,
                source_reference="WhatsApp community group #12",
            ),
            ResearchEvidenceItem(
                statement="High frequency of inquiries implies demand exceeds current supplier visibility",
                category=EvidenceCategory.INFERENCE,
                source_reference="Comparison between request count and supplier listings",
            ),
        ],
    )

    opp = OpportunityIngestionService.ingest(session, payload)

    # Source references are preserved
    assert "Primary field survey" in opp.source
    assert "WhatsApp community group #12" in opp.source
    assert "Comparison between request count and supplier listings" in opp.source

    # Evidence notes maintain cognitive categories and provenance
    assert "[FACT]" in opp.evidence_notes
    assert "[INFERENCE]" in opp.evidence_notes
    assert "Initial exploratory notes" in opp.evidence_notes


# ── Tests 4 to 7: Evidence Item Validation & Category Preservation ────────────────

def test_fact_and_inference_require_source_reference():
    """Test 4: FACT and INFERENCE evidence items require non-empty source_reference."""
    with pytest.raises(ValidationError) as exc:
        ResearchEvidenceItem(
            statement="Verified market size stat",
            category=EvidenceCategory.FACT,
            source_reference="",
        )
    assert "source_reference must not be empty" in str(exc.value)

    with pytest.raises(ValidationError) as exc_ws:
        ResearchEvidenceItem(
            statement="Derived deduction",
            category=EvidenceCategory.INFERENCE,
            source_reference="   ",
        )
    assert "source_reference must not be empty" in str(exc_ws.value)


def test_empty_statement_rejected():
    """Test 5: Empty/whitespace statement is rejected on ResearchEvidenceItem."""
    with pytest.raises(ValidationError) as exc:
        ResearchEvidenceItem(
            statement="   ",
            category=EvidenceCategory.FACT,
            source_reference="https://source.url",
        )
    assert "statement must not be empty" in str(exc.value)


def test_hypothesis_preserved_without_silent_conversion(session: Session):
    """Test 7: HYPOTHESIS is preserved explicitly as HYPOTHESIS."""
    item = ResearchEvidenceItem(
        statement="A one-click setup script will increase trial conversions from 3% to 7%",
        category=EvidenceCategory.HYPOTHESIS,
        source_reference="Product team brainstorming doc",
    )
    assert item.category == EvidenceCategory.HYPOTHESIS
    assert item.category.value == "hypothesis"

    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="DevOps Setup Script",
            description="Automated cluster bootstrapping.",
            category=OpportunityCategory.PRODUCT,
        ),
        evidence_items=[item],
    )

    opp = OpportunityIngestionService.ingest(session, payload)
    assert "[HYPOTHESIS]" in opp.evidence_notes
    assert "[FACT]" not in opp.evidence_notes


# ── Tests 8 to 9: Opportunity Contract & Repository Reuse ────────────────────────

def test_existing_opportunity_validation_is_respected():
    """Test 8: Underlying Opportunity validation is respected (e.g. missing required fields or invalid category)."""
    # Missing required title
    with pytest.raises(ValidationError) as exc_missing:
        OpportunityIngestionPayload(
            opportunity={"description": "Valid description", "category": "product"},  # type: ignore[arg-type]
            evidence_items=[],
        )
    assert "title" in str(exc_missing.value)

    # Invalid category
    with pytest.raises(ValidationError) as exc_cat:
        OpportunityIngestionPayload(
            opportunity={"title": "T", "description": "D", "category": "invalid_category"},  # type: ignore[arg-type]
            evidence_items=[],
        )
    assert "category" in str(exc_cat.value)



def test_persisted_via_opportunity_repository(session: Session):
    """Test 9: Ingested opportunity is retrievable through OpportunityRepository."""
    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="Retrievable Opp",
            description="Desc",
            category=OpportunityCategory.PRODUCT,
        ),
        evidence_items=[],
    )

    ingested = OpportunityIngestionService.ingest(session, payload)

    repo = OpportunityRepository(session)
    fetched = repo.get(ingested.id)
    assert fetched is not None
    assert fetched.id == ingested.id
    assert fetched.title == "Retrievable Opp"


# ── Tests 10 to 13: Side-Effect and Isolation Verification ─────────────────────────

def test_ingestion_causes_zero_capital_side_effects(session: Session):
    """Test 10: Ingestion causes zero capital transactions and does not touch balance."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    summary_before = cap_repo.get_financial_summary()
    tx_count_before = len(cap_repo.get_transaction_history())

    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="Capital Safe Opp",
            description="Desc",
            category=OpportunityCategory.PRODUCT,
        ),
        evidence_items=[
            ResearchEvidenceItem(
                statement="Customer demand verified",
                category=EvidenceCategory.FACT,
                source_reference="User survey",
            )
        ],
    )
    OpportunityIngestionService.ingest(session, payload)

    summary_after = cap_repo.get_financial_summary()
    tx_count_after = len(cap_repo.get_transaction_history())

    assert tx_count_after == tx_count_before
    assert summary_after.current_balance == summary_before.current_balance
    assert summary_after.total_cost == summary_before.total_cost
    assert summary_after.total_allocated == summary_before.total_allocated


def test_ingestion_creates_zero_experiments_or_decisions(session: Session):
    """Test 11 & 12: Ingestion creates zero experiments and zero decisions."""
    exp_repo = ExperimentRepository(session)
    dec_repo = DecisionRepository(session)

    exp_count_before = len(exp_repo.list())
    dec_count_before = len(dec_repo.list())

    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="Isolated Opp",
            description="Desc",
            category=OpportunityCategory.PRODUCT,
        ),
        evidence_items=[],
    )
    OpportunityIngestionService.ingest(session, payload)

    exp_count_after = len(exp_repo.list())
    dec_count_after = len(dec_repo.list())

    assert exp_count_after == exp_count_before
    assert dec_count_after == dec_count_before


def test_ingestion_does_not_modify_existing_opportunities(session: Session):
    """Test 13: Ingestion of a new opportunity leaves existing opportunities untouched."""
    opp_repo = OpportunityRepository(session)
    existing_opp = opp_repo.create(
        Opportunity(
            title="Existing Pre-stored Opp",
            description="Original description",
            category=OpportunityCategory.CONTENT,
            status=OpportunityStatus.UNDER_REVIEW,
        )
    )

    payload = OpportunityIngestionPayload(
        opportunity=Opportunity(
            title="Brand New Opp",
            description="New description",
            category=OpportunityCategory.FREELANCE,
        ),
        evidence_items=[],
    )
    OpportunityIngestionService.ingest(session, payload)

    re_fetched = opp_repo.get(existing_opp.id)
    assert re_fetched is not None
    assert re_fetched.title == "Existing Pre-stored Opp"
    assert re_fetched.status == OpportunityStatus.UNDER_REVIEW
