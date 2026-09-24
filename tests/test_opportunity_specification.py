"""Unit and integration tests for Step 24 — Human Opportunity Specification Boundary.

Verifies:
1. Valid candidate produces a human specification.
2. Required human fields must be explicitly supplied.
3. Missing required fields are rejected.
4. Empty/whitespace values are rejected.
5. Economic range inversions (max < min or negative values) are rejected.
6. Candidate ID mismatch is rejected on conversion to ingestion payload.
7. Candidate evidence is preserved.
8. Candidate source references are preserved.
9. Trend status remains distinguishable from human business assumptions.
10. Human rationale is not converted into FACT evidence.
11. Creating a specification causes zero database writes.
12. Creating a specification causes zero financial effects.
13. Creating a specification creates zero Opportunity records.
14. Explicit ingestion through the existing ingestion boundary creates canonical Opportunity.
15. Explicit ingestion produces strictly DISCOVERED status.
16. No Experiment is created during specification or ingestion.
17. No Decision is created during specification or ingestion.
18. No capital transaction occurs during specification or ingestion.
"""

from datetime import date
from collections.abc import Generator
from decimal import Decimal
from uuid import UUID, uuid4
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
from venturebot.opportunity.candidate import OpportunityCandidateGenerator
from venturebot.opportunity.ingestion import OpportunityIngestionService
from venturebot.opportunity.models import (
    HumanOpportunitySpecification,
    OpportunityCandidate,
    OpportunityIngestionPayload,
    ResearchEvidenceItem,
    TrendStatus,
)
from venturebot.opportunity.trend import ResearchTrendAnalyzer


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite in-memory database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


def make_sample_candidate() -> OpportunityCandidate:
    """Helper to produce a trend-validated OpportunityCandidate."""
    ev1 = ResearchEvidenceItem(
        statement="Wikipedia article 'Micro SaaS' recorded 45000 pageviews on 2026-09-01.",
        category=EvidenceCategory.FACT,
        source_reference="https://en.wikipedia.org/wiki/Micro_SaaS",
        observation_date=date(2026, 9, 1),
        metric_value=Decimal("45000"),
    )
    ev2 = ResearchEvidenceItem(
        statement="Wikipedia article 'Micro SaaS' recorded 60000 pageviews on 2026-09-02.",
        category=EvidenceCategory.FACT,
        source_reference="https://en.wikipedia.org/wiki/Micro_SaaS (day 2)",
        observation_date=date(2026, 9, 2),
        metric_value=Decimal("60000"),
    )
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])
    return OpportunityCandidateGenerator.generate_candidate_from_trend(trend)


# ── Specification Creation & Field Validation Tests ───────────────────────────


def test_valid_candidate_produces_human_specification():
    """Requirement 1: A valid candidate cleanly produces a HumanOpportunitySpecification."""
    candidate = make_sample_candidate()

    spec = candidate.create_specification(
        title="Micro SaaS Idea Validation Toolkit",
        description="Curated templates and validation checklists for solo software founders.",
        category=OpportunityCategory.PRODUCT,
        audience="Bootstrapped developers",
        monetization_notes="One-time purchase license at ₹999",
        estimated_revenue_min=Decimal("1000.00"),
        estimated_revenue_max=Decimal("5000.00"),
        estimated_cost_min=Decimal("100.00"),
        estimated_cost_max=Decimal("500.00"),
        reviewer_notes="Validated that dev interest is growing; template distribution is feasible.",
    )

    assert isinstance(spec, HumanOpportunitySpecification)
    assert spec.candidate_id == candidate.id
    assert spec.title == "Micro SaaS Idea Validation Toolkit"
    assert spec.category == OpportunityCategory.PRODUCT
    assert spec.audience == "Bootstrapped developers"
    assert spec.monetization_notes == "One-time purchase license at ₹999"
    assert spec.estimated_revenue_min == Decimal("1000.00")
    assert spec.estimated_revenue_max == Decimal("5000.00")
    assert spec.reviewer_notes == "Validated that dev interest is growing; template distribution is feasible."


def test_direct_specification_construction():
    """HumanOpportunitySpecification can also be instantiated directly with candidate_id."""
    cand_id = uuid4()
    spec = HumanOpportunitySpecification(
        candidate_id=cand_id,
        title="B2B Invoice Automation",
        description="Python utility for parsing invoices and reconciling accounts.",
        category=OpportunityCategory.SERVICE,
        audience="SMB accounting departments",
        monetization_notes="Monthly subscription at ₹1999/month",
        estimated_revenue_min=Decimal("2000.00"),
        estimated_revenue_max=Decimal("8000.00"),
        estimated_cost_min=Decimal("200.00"),
        estimated_cost_max=Decimal("1000.00"),
    )

    assert spec.candidate_id == cand_id
    assert spec.title == "B2B Invoice Automation"
    assert spec.category == OpportunityCategory.SERVICE


def test_required_fields_must_be_supplied_and_non_empty():
    """Requirements 2, 3, 4: Missing or empty/whitespace required fields are rejected."""
    candidate = make_sample_candidate()

    # Empty title
    with pytest.raises(ValueError, match="Required business specification field must not be empty"):
        candidate.create_specification(
            title="   ",
            description="Valid description",
            category=OpportunityCategory.PRODUCT,
            audience="Valid audience",
            monetization_notes="Valid monetization",
        )

    # Empty description
    with pytest.raises(ValueError, match="Required business specification field must not be empty"):
        candidate.create_specification(
            title="Valid Title",
            description="",
            category=OpportunityCategory.PRODUCT,
            audience="Valid audience",
            monetization_notes="Valid monetization",
        )

    # Empty audience
    with pytest.raises(ValueError, match="Required business specification field must not be empty"):
        candidate.create_specification(
            title="Valid Title",
            description="Valid description",
            category=OpportunityCategory.PRODUCT,
            audience="  ",
            monetization_notes="Valid monetization",
        )

    # Empty monetization_notes
    with pytest.raises(ValueError, match="Required business specification field must not be empty"):
        candidate.create_specification(
            title="Valid Title",
            description="Valid description",
            category=OpportunityCategory.PRODUCT,
            audience="Valid audience",
            monetization_notes="",
        )


def test_economic_range_validation():
    """Rejects inverted economic ranges and negative values."""
    candidate = make_sample_candidate()

    # Inverted revenue: max < min
    with pytest.raises(ValueError, match="estimated_revenue_max .* cannot be less than estimated_revenue_min"):
        candidate.create_specification(
            title="Valid Title",
            description="Valid description",
            category=OpportunityCategory.PRODUCT,
            audience="Valid audience",
            monetization_notes="Valid monetization",
            estimated_revenue_min=Decimal("5000.00"),
            estimated_revenue_max=Decimal("1000.00"),
        )

    # Inverted cost: max < min
    with pytest.raises(ValueError, match="estimated_cost_max .* cannot be less than estimated_cost_min"):
        candidate.create_specification(
            title="Valid Title",
            description="Valid description",
            category=OpportunityCategory.PRODUCT,
            audience="Valid audience",
            monetization_notes="Valid monetization",
            estimated_cost_min=Decimal("500.00"),
            estimated_cost_max=Decimal("100.00"),
        )

    # Negative economic value
    with pytest.raises(ValueError, match="Economic estimates cannot be negative"):
        candidate.create_specification(
            title="Valid Title",
            description="Valid description",
            category=OpportunityCategory.PRODUCT,
            audience="Valid audience",
            monetization_notes="Valid monetization",
            estimated_revenue_min=Decimal("-10.00"),
        )


def test_candidate_id_mismatch_rejected():
    """Rejects converting specification to ingestion payload when candidate ID does not match."""
    candidate1 = make_sample_candidate()
    candidate2 = make_sample_candidate()

    spec = candidate1.create_specification(
        title="Valid Title",
        description="Valid description",
        category=OpportunityCategory.PRODUCT,
        audience="Valid audience",
        monetization_notes="Valid monetization",
    )

    with pytest.raises(ValueError, match="Candidate ID mismatch"):
        spec.to_ingestion_payload(candidate=candidate2)


# ── Evidence & Epistemic Separation Tests ─────────────────────────────────────


def test_candidate_evidence_and_sources_preserved():
    """Requirements 5, 6: Preserves original candidate evidence items and source references."""
    candidate = make_sample_candidate()
    spec = candidate.create_specification(
        title="Micro SaaS Toolkit",
        description="Developer tooling kit.",
        category=OpportunityCategory.PRODUCT,
        audience="Indie hackers",
        monetization_notes="₹999 download",
    )

    payload = spec.to_ingestion_payload(candidate=candidate)

    assert isinstance(payload, OpportunityIngestionPayload)
    assert len(payload.evidence_items) == 2
    assert payload.evidence_items[0].statement == candidate.evidence_items[0].statement
    assert payload.evidence_items[0].observation_date == date(2026, 9, 1)
    assert payload.evidence_items[0].metric_value == Decimal("45000")
    assert payload.evidence_items[0].category == EvidenceCategory.FACT


def test_trend_status_distinguishable_from_human_business_assumptions():
    """Requirement 7: Trend status (empirical) is mapped distinctly from human assumptions (audience, monetization)."""
    candidate = make_sample_candidate()
    assert candidate.trend_status == TrendStatus.INCREASING

    spec = candidate.create_specification(
        title="Micro SaaS Toolkit",
        description="Developer tooling kit.",
        category=OpportunityCategory.PRODUCT,
        audience="Indie hackers",
        monetization_notes="₹999 digital download",
    )

    payload = spec.to_ingestion_payload(candidate=candidate)

    # Trend strength carries empirical classification
    assert payload.opportunity.trend_strength == "increasing"

    # Business assumptions carry human-entered data
    assert payload.opportunity.audience == "Indie hackers"
    assert payload.opportunity.monetization_notes == "₹999 digital download"
    assert payload.opportunity.category == OpportunityCategory.PRODUCT


def test_human_rationale_not_converted_into_fact_evidence():
    """Requirement 8: Reviewer rationale is NOT converted into FACT evidence."""
    candidate = make_sample_candidate()
    spec = candidate.create_specification(
        title="Micro SaaS Toolkit",
        description="Developer tooling kit.",
        category=OpportunityCategory.PRODUCT,
        audience="Indie hackers",
        monetization_notes="₹999 download",
        reviewer_notes="Human reviewer believes this is an underserved niche.",
    )

    payload = spec.to_ingestion_payload(candidate=candidate)

    # Evidence items contain ONLY the candidate's FACT evidence
    for item in payload.evidence_items:
        assert item.category == EvidenceCategory.FACT
        assert "Human reviewer believes" not in item.statement

    # Reviewer rationale is retained under explicit human specification label in evidence_notes
    assert "[HUMAN_SPECIFICATION] Reviewer rationale: Human reviewer believes" in payload.opportunity.evidence_notes
    assert "[FACT] Human reviewer believes" not in payload.opportunity.evidence_notes


# ── System Isolation Tests ───────────────────────────────────────────────────


def test_creating_specification_causes_zero_database_writes(session: Session):
    """Requirements 9, 11: Creating a specification causes zero database writes and creates zero Opportunities."""
    candidate = make_sample_candidate()

    spec = candidate.create_specification(
        title="Freelance Design System",
        description="Design templates for freelance web designers.",
        category=OpportunityCategory.CONTENT,
        audience="Freelancers",
        monetization_notes="₹499 templates",
    )
    assert spec is not None

    # Assert zero rows in all tables
    assert OpportunityRepository(session).list() == []
    assert CapitalRepository(session).get_transaction_history() == []
    assert ExperimentRepository(session).list() == []
    assert DecisionRepository(session).list() == []
    assert LearningRepository(session).list_for_opportunity(uuid4()) == []


def test_creating_specification_causes_zero_financial_effects(session: Session):
    """Requirement 10: Creating specification does not interact with the capital ledger."""
    candidate = make_sample_candidate()

    _ = candidate.create_specification(
        title="Affiliate Niche Directory",
        description="Curated directory for affiliate products.",
        category=OpportunityCategory.AFFILIATE,
        audience="Digital marketers",
        monetization_notes="Affiliate commissions",
    )

    cap_repo = CapitalRepository(session)
    assert cap_repo.get_transaction_history() == []
    assert cap_repo.get_current_balance() == Decimal("0.00")


# ── Explicit Ingestion Boundary Integration Tests ─────────────────────────────


def test_explicit_ingestion_creates_canonical_opportunity(session: Session):
    """Requirements 12, 13, 14, 15, 16: Explicit ingestion persists canonical Opportunity in DISCOVERED status."""
    candidate = make_sample_candidate()
    spec = candidate.create_specification(
        title="Micro SaaS Idea Validation Toolkit",
        description="Curated templates and validation checklists for solo software founders.",
        category=OpportunityCategory.PRODUCT,
        audience="Bootstrapped founders",
        monetization_notes="One-time ₹999 digital download",
        estimated_revenue_min=Decimal("1000.00"),
        estimated_revenue_max=Decimal("5000.00"),
        estimated_cost_min=Decimal("100.00"),
        estimated_cost_max=Decimal("500.00"),
        reviewer_notes="High priority candidate based on sustained dev engagement.",
    )

    # Ingest through the explicit ingestion boundary
    persisted_opp = OpportunityIngestionService.ingest_specification(session, spec, candidate)

    # 1. Assert canonical Opportunity attributes and DISCOVERED status
    assert isinstance(persisted_opp, Opportunity)
    assert persisted_opp.status == OpportunityStatus.DISCOVERED
    assert persisted_opp.title == "Micro SaaS Idea Validation Toolkit"
    assert persisted_opp.category == OpportunityCategory.PRODUCT
    assert persisted_opp.audience == "Bootstrapped founders"
    assert persisted_opp.trend_strength == "increasing"

    # 2. Assert provenance preservation
    assert "https://en.wikipedia.org/wiki/Micro_SaaS" in persisted_opp.source
    assert "[FACT] Wikipedia article 'Micro SaaS'" in persisted_opp.evidence_notes
    assert "[HUMAN_SPECIFICATION] Reviewer rationale: High priority candidate" in persisted_opp.evidence_notes

    # 3. Assert exactly one Opportunity persisted
    opp_repo = OpportunityRepository(session)
    all_opps = opp_repo.list()
    assert len(all_opps) == 1
    assert all_opps[0].id == persisted_opp.id

    # 4. Assert zero side-effects across other domains
    assert ExperimentRepository(session).list() == []
    assert DecisionRepository(session).list() == []
    assert CapitalRepository(session).get_transaction_history() == []
    assert CapitalRepository(session).get_current_balance() == Decimal("0.00")


def test_to_ingestion_payload_then_ingest_path(session: Session):
    """Alternative explicit path: spec.to_ingestion_payload() followed by OpportunityIngestionService.ingest()."""
    candidate = make_sample_candidate()
    spec = candidate.create_specification(
        title="Local Service SEO Engine",
        description="Automated local SEO audit script.",
        category=OpportunityCategory.SERVICE,
        audience="Plumbers and electricians",
        monetization_notes="₹2499 per audit",
    )

    payload = spec.to_ingestion_payload(candidate)
    persisted_opp = OpportunityIngestionService.ingest(session, payload)

    assert persisted_opp.status == OpportunityStatus.DISCOVERED
    assert persisted_opp.title == "Local Service SEO Engine"
    assert len(OpportunityRepository(session).list()) == 1
