"""Tests for Step 21 — Research Evidence to Opportunity Candidate Foundation.

Verifies:
1. Valid research evidence deterministically produces an OpportunityCandidate.
2. Candidate retains original source evidence without loss or corruption.
3. FACT classification is preserved and clearly distinguishable from derived inferences.
4. Hypothesis is explicitly represented as a hypothesis.
5. Unknown/validation areas are explicitly enumerated.
6. Empty evidence results are handled deterministically.
7. Invalid evidence inputs are rejected with clear ValueError exceptions.
8. Candidate generation causes zero database writes across all persistent tables.
9. Candidate generation does not create canonical Opportunity records.
10. Candidate generation does not create Experiment records.
11. Candidate generation does not interact with the financial ledger.
12. No scores, rankings, or arbitrary thresholds are generated.
13. Packaging into OpportunityIngestionPayload functions cleanly for explicit human ingestion.
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
    OpportunityCandidate,
    OpportunityIngestionPayload,
    ResearchCollectionResult,
    ResearchEvidenceItem,
    ResearchTrendObservation,
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


def make_sample_evidence(topic: str = "Artificial intelligence", views: int = 450123, rank: int = 3) -> ResearchEvidenceItem:
    """Helper to produce standard FACT evidence item."""
    return ResearchEvidenceItem(
        statement=f"Wikipedia article '{topic}' recorded {views} pageviews (rank #{rank}) on 2026-09-16.",
        category=EvidenceCategory.FACT,
        source_reference=f"https://en.wikipedia.org/wiki/{topic.replace(' ', '_')} (via https://wikimedia.org/api/rest_v1/metrics/pageviews/top)",
    )


# ── Candidate Generation & Epistemic Separation Tests ─────────────────────────

def test_valid_evidence_produces_opportunity_candidate():
    """Valid ResearchEvidenceItem deterministically produces a structured OpportunityCandidate."""
    evidence = make_sample_evidence("Formula One", views=312450, rank=4)

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    assert isinstance(candidate, OpportunityCandidate)
    assert isinstance(candidate.id, UUID)
    assert candidate.topic == "Formula One"
    assert candidate.title == "Opportunity Candidate: Formula One"
    assert candidate.evidence_count == 1
    assert candidate.source_references == [evidence.source_reference]


def test_candidate_retains_original_source_evidence():
    """Original ResearchEvidenceItem objects are preserved intact inside the candidate."""
    evidence = make_sample_evidence("OpenAI", views=85000, rank=10)

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    assert len(candidate.evidence_items) == 1
    retained_item = candidate.evidence_items[0]
    assert retained_item.statement == evidence.statement
    assert retained_item.category == EvidenceCategory.FACT
    assert retained_item.source_reference == evidence.source_reference


def test_fact_remains_distinguishable_from_derived_inference():
    """Original factual evidence is separated from derived observation and inference."""
    evidence = make_sample_evidence("Renewable Energy", views=120000, rank=15)

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    # 1. FACT is in evidence_items
    assert candidate.evidence_items[0].category == EvidenceCategory.FACT
    assert "Wikipedia article 'Renewable Energy' recorded 120000 pageviews" in candidate.evidence_items[0].statement

    # 2. OBSERVATION quotes empirical attention
    assert "Empirical observation recorded" in candidate.observation
    assert "120000 pageviews" in candidate.observation

    # 3. INFERENCE deduces public interest / visibility only
    assert "measurable public attention" in candidate.inference
    assert "Renewable Energy" in candidate.inference
    assert "exploratory opportunity investigation" in candidate.inference

    # Verify no fabricated business or trend claims exist in inference
    assert "profitable" not in candidate.inference.lower()
    assert "revenue" not in candidate.inference.lower()
    assert "high demand" not in candidate.inference.lower()
    assert "sustained" not in candidate.inference.lower()
    assert "surging" not in candidate.inference.lower()
    assert "increasing" not in candidate.inference.lower()


def test_single_evidence_item_does_not_claim_sustained_or_surging_trend():
    """Step 21A: A single evidence observation must never produce sustained or surging trend claims."""
    evidence = make_sample_evidence("Generative AI", views=500000, rank=1)

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    inference_lower = candidate.inference.lower()
    assert "sustained" not in inference_lower
    assert "surging" not in inference_lower
    assert "increasing" not in inference_lower
    assert "rising" not in inference_lower
    assert "trend" not in inference_lower
    assert "measurable public attention in the recorded observation" in inference_lower


def test_hypothesis_is_explicitly_represented_as_hypothesis():
    """Hypothesis is explicitly formulated as a testable, unconfirmed premise."""
    evidence = make_sample_evidence("Podcast Hosting", views=45000, rank=50)

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    assert "may exist" in candidate.hypothesis
    assert "subject to commercial validation" in candidate.hypothesis
    # Verify it does not assert certainty
    assert "guaranteed" not in candidate.hypothesis.lower()
    assert "proven" not in candidate.hypothesis.lower()


def test_unknowns_are_explicitly_enumerated():
    """Unvalidated areas (willingness to pay, monetization, customer segments, economics) are explicit."""
    evidence = make_sample_evidence("Home Gardening", views=95000, rank=20)

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    assert len(candidate.unknowns) >= 4
    unknowns_str = " ".join(candidate.unknowns).lower()
    assert "willingness to pay" in unknowns_str
    assert "monetization models" in unknowns_str
    assert "customer segments" in unknowns_str
    assert "unit economics" in unknowns_str
    assert "unverified" in unknowns_str


def test_multiple_evidence_items_generate_single_candidate():
    """Multiple related evidence items compile cleanly into one candidate with distinct sources."""
    ev1 = make_sample_evidence("Cybersecurity", views=200000, rank=5)
    ev2 = ResearchEvidenceItem(
        statement="Wikipedia article 'Cybersecurity' recorded 220000 pageviews on 2026-09-17.",
        category=EvidenceCategory.FACT,
        source_reference="https://en.wikipedia.org/wiki/Cybersecurity (via endpoint_day_2)",
    )

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence([ev1, ev2])

    assert candidate.evidence_count == 2
    assert candidate.topic == "Cybersecurity"
    assert len(candidate.source_references) == 2
    assert "Multiple empirical observations (2 items)" in candidate.observation


def test_generate_candidates_from_collection_result():
    """Generates a list of candidates from a ResearchCollectionResult."""
    ev1 = make_sample_evidence("Robotics", views=300000, rank=2)
    ev2 = make_sample_evidence("Quantum Computing", views=150000, rank=8)

    collection = ResearchCollectionResult(
        source_id="wikimedia_pageviews",
        collection_date=date(2026, 9, 16),
        evidence_items=[ev1, ev2],
    )

    candidates = OpportunityCandidateGenerator.generate_candidates(collection)

    assert len(candidates) == 2
    assert candidates[0].topic == "Robotics"
    assert candidates[1].topic == "Quantum Computing"


# ── Error & Edge Case Handling Tests ──────────────────────────────────────────

def test_empty_evidence_handled_deterministically():
    """Empty inputs to generate_candidates return empty list, while generate_candidate_from_evidence raises ValueError."""
    # Empty collection returns empty list
    empty_collection = ResearchCollectionResult(
        source_id="wikimedia_pageviews",
        collection_date=date(2026, 9, 16),
        evidence_items=[],
    )
    assert OpportunityCandidateGenerator.generate_candidates(empty_collection) == []
    assert OpportunityCandidateGenerator.generate_candidates([]) == []

    # Empty evidence list passed directly raises ValueError
    with pytest.raises(ValueError, match="At least one ResearchEvidenceItem is required"):
        OpportunityCandidateGenerator.generate_candidate_from_evidence([])


def test_invalid_evidence_type_rejected():
    """Rejects non-evidence objects with clear ValueError."""
    with pytest.raises(ValueError, match="Expected ResearchEvidenceItem"):
        OpportunityCandidateGenerator.generate_candidate_from_evidence(["not an item"])  # type: ignore

    with pytest.raises(ValueError, match="research_input must be a ResearchCollectionResult"):
        OpportunityCandidateGenerator.generate_candidates("invalid input")  # type: ignore


def test_topic_extraction_fallback():
    """Extracts topic from quoted pattern or falls back gracefully to clean statement."""
    statement1 = "Wikipedia article 'Indie Game Development' recorded 5000 pageviews."
    assert OpportunityCandidateGenerator.extract_topic_from_statement(statement1) == "Indie Game Development"

    statement2 = "Plain text observation without quotes."
    assert OpportunityCandidateGenerator.extract_topic_from_statement(statement2) == statement2

    assert OpportunityCandidateGenerator.extract_topic_from_statement("   ") == "Unspecified Topic"


# ── Absence of Scoring & Evaluation Guardrail Tests ───────────────────────────

def test_no_scores_rankings_or_arbitrary_thresholds():
    """Candidate contract does not contain score, ranking, or arbitrary threshold attributes."""
    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(
        make_sample_evidence("WebAssembly", views=60000, rank=30)
    )

    # Verify absence of speculative scoring fields
    assert not hasattr(candidate, "score")
    assert not hasattr(candidate, "opportunity_score")
    assert not hasattr(candidate, "trend_score")
    assert not hasattr(candidate, "confidence_score")
    assert not hasattr(candidate, "rank")
    assert not hasattr(candidate, "ranking")
    assert not hasattr(candidate, "winner")


# ── Side-Effect & Domain Isolation Tests ───────────────────────────────────────

def test_candidate_generation_causes_zero_database_writes(session: Session):
    """Generating candidates causes zero database writes across all tables."""
    evidence = make_sample_evidence("Bioinformatics", views=70000, rank=25)

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)
    assert candidate is not None

    # Assert zero rows in all persistent tables
    assert OpportunityRepository(session).list() == []
    assert CapitalRepository(session).get_transaction_history() == []
    assert ExperimentRepository(session).list() == []
    assert DecisionRepository(session).list() == []
    assert LearningRepository(session).list_for_opportunity(uuid4()) == []


def test_candidate_generation_does_not_create_opportunity():
    """Candidate generation returns strictly OpportunityCandidate, never canonical Opportunity."""
    evidence = make_sample_evidence("3D Printing", views=80000, rank=22)

    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    assert isinstance(candidate, OpportunityCandidate)
    assert not isinstance(candidate, Opportunity)


def test_candidate_generation_causes_zero_financial_effects(session: Session):
    """Candidate generation has zero interaction with the capital ledger."""
    evidence = make_sample_evidence("Drone Photography", views=90000, rank=18)

    OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    cap_repo = CapitalRepository(session)
    assert cap_repo.get_transaction_history() == []
    assert cap_repo.get_current_balance() == Decimal("0.00")


# ── Integration with OpportunityIngestionService (Caller Boundary) ─────────────

def test_to_ingestion_payload_enables_explicit_caller_ingestion(session: Session):
    """Candidate packages its verified evidence into an ingestion payload for explicit human-specified Opportunity creation."""
    evidence = make_sample_evidence("Micro SaaS", views=110000, rank=12)
    candidate = OpportunityCandidateGenerator.generate_candidate_from_evidence(evidence)

    # 1. Human reviewer specifies the business opportunity parameters
    specified_opp = Opportunity(
        title="Micro SaaS Idea Validation Toolkit",
        description="Curated templates and validation checklists for solo software founders.",
        category=OpportunityCategory.PRODUCT,
        status=OpportunityStatus.UNDER_REVIEW,  # Service enforces DISCOVERED
        audience="Bootstrapped founders",
        monetization_notes="One-time ₹999 digital download",
        estimated_revenue_min=Decimal("1000.00"),
        estimated_revenue_max=Decimal("5000.00"),
        estimated_cost_min=Decimal("100.00"),
        estimated_cost_max=Decimal("500.00"),
    )

    # 2. Package candidate evidence with specified Opportunity
    payload = candidate.to_ingestion_payload(specified_opp)
    assert isinstance(payload, OpportunityIngestionPayload)
    assert payload.opportunity == specified_opp
    assert payload.evidence_items == candidate.evidence_items

    # 3. Explicit ingestion write
    persisted_opp = OpportunityIngestionService.ingest(session, payload)

    # 4. Assert canonical Opportunity invariants
    assert persisted_opp.status == OpportunityStatus.DISCOVERED
    assert "[FACT] Wikipedia article 'Micro SaaS'" in persisted_opp.evidence_notes
    assert "https://en.wikipedia.org/wiki/Micro_SaaS" in persisted_opp.source

    # 5. Exactly one Opportunity persisted
    opp_repo = OpportunityRepository(session)
    all_opps = opp_repo.list()
    assert len(all_opps) == 1
    assert all_opps[0].id == persisted_opp.id

    # 6. Financial ledger remains untouched
    assert CapitalRepository(session).get_transaction_history() == []


# ── Step 23: Trend-Validated Opportunity Candidate Integration Tests ──────────


def make_dated_evidence(
    topic: str,
    obs_date: date,
    views: int,
    source_ref: str | None = None,
) -> ResearchEvidenceItem:
    """Helper for dated evidence items in trend integration tests."""
    return ResearchEvidenceItem(
        statement=f"Wikipedia article '{topic}' recorded {views} pageviews on {obs_date.isoformat()}.",
        category=EvidenceCategory.FACT,
        source_reference=source_ref or f"https://en.wikipedia.org/wiki/{topic.replace(' ', '_')}",
        observation_date=obs_date,
        metric_value=Decimal(views),
    )


def test_increasing_trend_produces_candidate():
    """Requirement 1: Increasing trend produces an OpportunityCandidate with trend_status INCREASING."""
    ev1 = make_dated_evidence("Cybersecurity", date(2026, 9, 1), 50000)
    ev2 = make_dated_evidence("Cybersecurity", date(2026, 9, 2), 75000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    assert isinstance(candidate, OpportunityCandidate)
    assert candidate.trend_status == TrendStatus.INCREASING
    assert candidate.topic == "Cybersecurity"
    assert "classified as increasing" in candidate.inference
    assert "increased from 50000 on 2026-09-01 to 75000 on 2026-09-02" in candidate.inference
    assert candidate.evidence_count == 2


def test_decreasing_trend_produces_candidate():
    """Requirement 2: Decreasing trend produces an OpportunityCandidate with trend_status DECREASING."""
    ev1 = make_dated_evidence("Remote Work", date(2026, 9, 1), 90000)
    ev2 = make_dated_evidence("Remote Work", date(2026, 9, 2), 60000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    assert isinstance(candidate, OpportunityCandidate)
    assert candidate.trend_status == TrendStatus.DECREASING
    assert candidate.topic == "Remote Work"
    assert "classified as decreasing" in candidate.inference
    assert "decreased from 90000 on 2026-09-01 to 60000 on 2026-09-02" in candidate.inference


def test_stable_trend_produces_candidate():
    """Requirement 3: Stable trend produces an OpportunityCandidate with trend_status STABLE."""
    ev1 = make_dated_evidence("Open Source", date(2026, 9, 1), 40000)
    ev2 = make_dated_evidence("Open Source", date(2026, 9, 2), 40000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    assert isinstance(candidate, OpportunityCandidate)
    assert candidate.trend_status == TrendStatus.STABLE
    assert candidate.topic == "Open Source"
    assert "classified as stable" in candidate.inference
    assert "remained constant at 40000" in candidate.inference


def test_no_directional_change_trend_produces_candidate():
    """Requirement 4: Fluctuating trend produces an OpportunityCandidate with NO_DIRECTIONAL_CHANGE."""
    ev1 = make_dated_evidence("Electric Vehicles", date(2026, 9, 1), 100000)
    ev2 = make_dated_evidence("Electric Vehicles", date(2026, 9, 2), 150000)
    ev3 = make_dated_evidence("Electric Vehicles", date(2026, 9, 3), 80000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2, ev3])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    assert isinstance(candidate, OpportunityCandidate)
    assert candidate.trend_status == TrendStatus.NO_DIRECTIONAL_CHANGE
    assert candidate.topic == "Electric Vehicles"
    assert "classified as no_directional_change" in candidate.inference
    assert "fluctuated between 2026-09-01 and 2026-09-03" in candidate.inference


def test_insufficient_data_trend_remains_explicitly_identifiable():
    """Requirement 5: Insufficient data trend produces a candidate with INSUFFICIENT_DATA status."""
    ev = make_dated_evidence("Clean Tech", date(2026, 9, 1), 1000)
    trend = ResearchTrendAnalyzer.analyze_trend(ev)

    candidate = OpportunityCandidateGenerator.generate_from_trend(trend)

    assert isinstance(candidate, OpportunityCandidate)
    assert candidate.trend_status == TrendStatus.INSUFFICIENT_DATA
    assert "classified as insufficient_data" in candidate.inference
    assert "Single observation recorded on 2026-09-01" in candidate.inference


def test_candidate_preserves_original_evidence_dates_and_metric_values():
    """Requirements 6, 7, 8: Original ResearchEvidenceItem objects, dates, and metric values are preserved."""
    ev1 = make_dated_evidence("Nanotechnology", date(2026, 9, 1), 30000)
    ev2 = make_dated_evidence("Nanotechnology", date(2026, 9, 2), 35000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    assert candidate.evidence_count == 2
    assert candidate.evidence_items[0].observation_date == date(2026, 9, 1)
    assert candidate.evidence_items[0].metric_value == Decimal("30000")
    assert candidate.evidence_items[1].observation_date == date(2026, 9, 2)
    assert candidate.evidence_items[1].metric_value == Decimal("35000")
    assert candidate.evidence_items[0].category == EvidenceCategory.FACT


def test_candidate_preserves_source_references():
    """Requirement 9: Preserves source references across trend and candidate."""
    ev1 = make_dated_evidence("AI Chips", date(2026, 9, 1), 50000, source_ref="https://source1.example.com")
    ev2 = make_dated_evidence("AI Chips", date(2026, 9, 2), 60000, source_ref="https://source2.example.com")
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    assert len(candidate.source_references) == 2
    assert "https://source1.example.com" in candidate.source_references
    assert "https://source2.example.com" in candidate.source_references


def test_trend_status_distinguishable_from_commercial_hypothesis():
    """Requirement 10: Trend status describes measurements, clearly distinct from unvalidated hypothesis."""
    ev1 = make_dated_evidence("Solar Energy", date(2026, 9, 1), 20000)
    ev2 = make_dated_evidence("Solar Energy", date(2026, 9, 2), 30000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    # Trend status is objective classification
    assert candidate.trend_status == TrendStatus.INCREASING

    # Hypothesis explicitly asserts unvalidated potential
    assert "may exist, subject to commercial validation" in candidate.hypothesis
    assert "subject to commercial validation" in candidate.hypothesis

    # Unknowns explicitly enumerate commercial unverified factors
    assert any("willingness to pay" in u for u in candidate.unknowns)
    assert any("monetization models" in u for u in candidate.unknowns)


def test_no_market_demand_or_commercial_claims_generated_from_trend():
    """Requirements 11, 12, 13: Candidate does NOT claim market demand, customer demand, profitability, or revenue."""
    ev1 = make_dated_evidence("EdTech", date(2026, 9, 1), 50000)
    ev2 = make_dated_evidence("EdTech", date(2026, 9, 2), 90000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    full_text = (
        f"{candidate.observation} {candidate.inference} {candidate.hypothesis} "
        f"{' '.join(candidate.unknowns)}"
    ).lower()

    assert "market demand" not in full_text
    assert "customer demand" not in full_text
    assert "willingness to pay" in " ".join(candidate.unknowns).lower()  # Only as UNKNOWN
    assert "high demand" not in full_text
    assert "profitable" not in full_text
    assert "revenue" not in full_text
    assert "winning niche" not in full_text
    assert "guaranteed" not in full_text
    assert "reliable market" not in full_text
    assert "bad opportunity" not in full_text


def test_no_scores_rankings_or_confidence_fields_in_trend_candidate():
    """Requirement 14: Zero score, rank, weight, or confidence attributes exist on candidate."""
    ev1 = make_dated_evidence("Bioinformatics", date(2026, 9, 1), 10000)
    ev2 = make_dated_evidence("Bioinformatics", date(2026, 9, 2), 20000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    assert not hasattr(candidate, "score")
    assert not hasattr(candidate, "opportunity_score")
    assert not hasattr(candidate, "trend_score")
    assert not hasattr(candidate, "confidence")
    assert not hasattr(candidate, "confidence_score")
    assert not hasattr(candidate, "rank")
    assert not hasattr(candidate, "ranking")
    assert not hasattr(candidate, "weight")


def test_trend_candidate_causes_zero_database_writes(session: Session):
    """Requirement 15: Generating candidate from trend causes zero SQLite writes."""
    ev1 = make_dated_evidence("AgriTech", date(2026, 9, 1), 10000)
    ev2 = make_dated_evidence("AgriTech", date(2026, 9, 2), 20000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)
    assert candidate is not None

    assert OpportunityRepository(session).list() == []
    assert CapitalRepository(session).get_transaction_history() == []
    assert ExperimentRepository(session).list() == []
    assert DecisionRepository(session).list() == []
    assert LearningRepository(session).list_for_opportunity(uuid4()) == []


def test_trend_candidate_causes_zero_financial_effects(session: Session):
    """Requirement 16: Zero financial interactions or balance changes."""
    ev1 = make_dated_evidence("FinTech", date(2026, 9, 1), 5000)
    ev2 = make_dated_evidence("FinTech", date(2026, 9, 2), 8000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    cap_repo = CapitalRepository(session)
    assert cap_repo.get_transaction_history() == []
    assert cap_repo.get_current_balance() == Decimal("0.00")


def test_trend_candidate_does_not_create_opportunity_or_experiment():
    """Requirements 17, 18: Returns OpportunityCandidate, never Opportunity or Experiment."""
    ev1 = make_dated_evidence("HealthTech", date(2026, 9, 1), 15000)
    ev2 = make_dated_evidence("HealthTech", date(2026, 9, 2), 25000)
    trend = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    candidate = OpportunityCandidateGenerator.generate_candidate_from_trend(trend)

    assert isinstance(candidate, OpportunityCandidate)
    assert not isinstance(candidate, Opportunity)
    assert not hasattr(candidate, "phase")
    assert not hasattr(candidate, "tier")
    assert not hasattr(candidate, "allocated_budget")
    assert not hasattr(candidate, "experiment_id")


def test_generate_candidates_supports_trend_observation_and_sequences():
    """generate_candidates() polymorphically supports single or sequences of ResearchTrendObservation."""
    ev1 = make_dated_evidence("Drones", date(2026, 9, 1), 10000)
    ev2 = make_dated_evidence("Drones", date(2026, 9, 2), 20000)
    trend1 = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    ev3 = make_dated_evidence("Satellites", date(2026, 9, 1), 30000)
    ev4 = make_dated_evidence("Satellites", date(2026, 9, 2), 15000)
    trend2 = ResearchTrendAnalyzer.analyze_trend([ev3, ev4])

    # Single trend observation
    res_single = OpportunityCandidateGenerator.generate_candidates(trend1)
    assert len(res_single) == 1
    assert res_single[0].topic == "Drones"
    assert res_single[0].trend_status == TrendStatus.INCREASING

    # Sequence of trend observations
    res_seq = OpportunityCandidateGenerator.generate_candidates([trend1, trend2])
    assert len(res_seq) == 2
    assert res_seq[0].topic == "Drones"
    assert res_seq[0].trend_status == TrendStatus.INCREASING
    assert res_seq[1].topic == "Satellites"
    assert res_seq[1].trend_status == TrendStatus.DECREASING


def test_invalid_trend_observation_rejected():
    """Rejects invalid trend observation argument with descriptive ValueError."""
    with pytest.raises(ValueError, match="Expected ResearchTrendObservation"):
        OpportunityCandidateGenerator.generate_candidate_from_trend("not a trend")  # type: ignore
