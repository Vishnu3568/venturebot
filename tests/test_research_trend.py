"""Unit and integration tests for Step 22 — Research Trend Validation Foundation.

Validates:
1. Single dated observation -> INSUFFICIENT_DATA
2. Two dated observations with higher later value -> INCREASING
3. Two dated observations with lower later value -> DECREASING
4. Equal observations -> STABLE
5. Fluctuating observations -> NO_DIRECTIONAL_CHANGE
6. Chronologically unordered input is handled deterministically
7. Missing/invalid dates are rejected explicitly
8. Missing/invalid metric values are rejected explicitly
9. Conflicting values on same date are rejected
10. Topic extraction and explicit override
11. Original evidence items and source references are preserved
12. Strict epistemic separation: no commercial hypotheses, market demand claims, or revenue claims
13. Absence of speculative scoring, ranking, or confidence ratings
14. Complete isolation: zero database writes, zero financial effects, zero Opportunity/Experiment creation
"""

from collections.abc import Generator
from datetime import date
from decimal import Decimal
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
from venturebot.opportunity.models import (
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


def make_evidence(
    topic: str,
    obs_date: date | None,
    views: int | Decimal | None = None,
    statement: str | None = None,
    source_ref: str | None = None,
) -> ResearchEvidenceItem:
    """Helper to build a deterministic ResearchEvidenceItem for testing."""
    views_val = Decimal(views) if views is not None else None
    display_statement = (
        statement
        if statement is not None
        else f"Wikipedia article '{topic}' recorded {views} pageviews on {obs_date.isoformat() if obs_date else 'unknown'}."
    )
    return ResearchEvidenceItem(
        statement=display_statement,
        category=EvidenceCategory.FACT,
        source_reference=source_ref or f"https://en.wikipedia.org/wiki/{topic.replace(' ', '_')}",
        observation_date=obs_date,
        metric_value=views_val,
    )


# ── Directional Trend Determination Tests ─────────────────────────────────────


def test_single_dated_observation_returns_insufficient_data():
    """Requirement 1: Single dated observation must evaluate to INSUFFICIENT_DATA."""
    item = make_evidence("Clean Tech", date(2026, 9, 1), views=1000)

    result = ResearchTrendAnalyzer.analyze_trend(item)

    assert isinstance(result, ResearchTrendObservation)
    assert result.status == TrendStatus.INSUFFICIENT_DATA
    assert result.topic == "Clean Tech"
    assert "Single observation recorded on 2026-09-01" in result.analysis
    assert "minimum of two dated observations" in result.analysis.lower()
    assert result.evidence_count == 1


def test_two_dated_observations_with_higher_later_value_returns_increasing():
    """Requirement 2: Two dated observations with higher later value must evaluate to INCREASING."""
    ev1 = make_evidence("Cybersecurity", date(2026, 9, 1), views=50000)
    ev2 = make_evidence("Cybersecurity", date(2026, 9, 2), views=75000)

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    assert result.status == TrendStatus.INCREASING
    assert result.topic == "Cybersecurity"
    assert "increased from 50000 on 2026-09-01 to 75000 on 2026-09-02" in result.analysis
    assert "+25000" in result.analysis
    assert result.evidence_count == 2


def test_two_dated_observations_with_lower_later_value_returns_decreasing():
    """Requirement 3: Two dated observations with lower later value must evaluate to DECREASING."""
    ev1 = make_evidence("Remote Work", date(2026, 9, 1), views=90000)
    ev2 = make_evidence("Remote Work", date(2026, 9, 2), views=60000)

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    assert result.status == TrendStatus.DECREASING
    assert result.topic == "Remote Work"
    assert "decreased from 90000 on 2026-09-01 to 60000 on 2026-09-02" in result.analysis
    assert "-30000" in result.analysis
    assert result.evidence_count == 2


def test_equal_observations_returns_stable():
    """Requirement 4: Equal observations across dates must evaluate to STABLE."""
    ev1 = make_evidence("Open Source", date(2026, 9, 1), views=40000)
    ev2 = make_evidence("Open Source", date(2026, 9, 2), views=40000)
    ev3 = make_evidence("Open Source", date(2026, 9, 3), views=40000)

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2, ev3])

    assert result.status == TrendStatus.STABLE
    assert result.topic == "Open Source"
    assert "remained constant at 40000" in result.analysis
    assert result.evidence_count == 3


def test_fluctuating_observations_returns_no_directional_change():
    """Fluctuating observations across multiple dates must evaluate to NO_DIRECTIONAL_CHANGE."""
    ev1 = make_evidence("Electric Vehicles", date(2026, 9, 1), views=100000)
    ev2 = make_evidence("Electric Vehicles", date(2026, 9, 2), views=150000)
    ev3 = make_evidence("Electric Vehicles", date(2026, 9, 3), views=80000)

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2, ev3])

    assert result.status == TrendStatus.NO_DIRECTIONAL_CHANGE
    assert "fluctuated between 2026-09-01 and 2026-09-03" in result.analysis
    assert "without a consistent directional change" in result.analysis


# ── Ordering & Determinism Tests ──────────────────────────────────────────────


def test_chronologically_unordered_input_handled_deterministically():
    """Requirement 5: Chronologically unordered input is sorted ascending and produces identical result."""
    ev_early = make_evidence("Robotics", date(2026, 9, 1), views=20000)
    ev_mid = make_evidence("Robotics", date(2026, 9, 5), views=35000)
    ev_late = make_evidence("Robotics", date(2026, 9, 10), views=50000)

    # Supply in reverse chronological order
    result_reversed = ResearchTrendAnalyzer.analyze_trend([ev_late, ev_early, ev_mid])
    # Supply in chronological order
    result_ordered = ResearchTrendAnalyzer.analyze_trend([ev_early, ev_mid, ev_late])

    assert result_reversed.status == TrendStatus.INCREASING
    assert result_reversed.status == result_ordered.status
    assert result_reversed.analysis == result_ordered.analysis
    assert result_reversed.fact_summary == result_ordered.fact_summary
    assert [it.observation_date for it in result_reversed.evidence_items] == [
        date(2026, 9, 1),
        date(2026, 9, 5),
        date(2026, 9, 10),
    ]


# ── Validation & Error Handling Tests ─────────────────────────────────────────


def test_missing_observation_date_rejected_with_value_error():
    """Requirement 6: Evidence items missing observation_date are rejected explicitly."""
    ev_without_date = make_evidence("Quantum Computing", None, views=15000)

    with pytest.raises(ValueError, match="missing observation_date"):
        ResearchTrendAnalyzer.analyze_trend(ev_without_date)


def test_invalid_input_types_rejected():
    """Rejects strings, non-evidence items, and empty lists with descriptive ValueError."""
    with pytest.raises(ValueError, match="evidence must be a ResearchEvidenceItem or a sequence"):
        ResearchTrendAnalyzer.analyze_trend("string input")  # type: ignore

    with pytest.raises(ValueError, match="Expected ResearchEvidenceItem"):
        ResearchTrendAnalyzer.analyze_trend([123, 456])  # type: ignore

    with pytest.raises(ValueError, match="At least one ResearchEvidenceItem is required"):
        ResearchTrendAnalyzer.analyze_trend([])


def test_conflicting_observations_on_same_date_rejected():
    """Conflicting measurements on the exact same date are rejected to prevent ambiguous trends."""
    ev1 = make_evidence("Generative AI", date(2026, 9, 1), views=100000)
    ev2 = make_evidence("Generative AI", date(2026, 9, 1), views=200000)

    with pytest.raises(ValueError, match="Conflicting observations recorded on the same date"):
        ResearchTrendAnalyzer.analyze_trend([ev1, ev2])


def test_metric_value_extracted_from_statement_fallback():
    """Extracts numeric metric from statement when metric_value is None."""
    item1 = ResearchEvidenceItem(
        statement="Wikipedia article 'Microbiome' recorded 45000 pageviews on 2026-09-01.",
        category=EvidenceCategory.FACT,
        source_reference="https://en.wikipedia.org/wiki/Microbiome",
        observation_date=date(2026, 9, 1),
        metric_value=None,
    )
    item2 = ResearchEvidenceItem(
        statement="Wikipedia article 'Microbiome' recorded 60000 pageviews on 2026-09-02.",
        category=EvidenceCategory.FACT,
        source_reference="https://en.wikipedia.org/wiki/Microbiome",
        observation_date=date(2026, 9, 2),
        metric_value=None,
    )

    result = ResearchTrendAnalyzer.analyze_trend([item1, item2])

    assert result.status == TrendStatus.INCREASING
    assert "45000" in result.analysis
    assert "60000" in result.analysis


def test_unresolvable_metric_value_rejected():
    """Rejects evidence item when neither metric_value nor numeric value exists in statement."""
    item = ResearchEvidenceItem(
        statement="Qualitative statement without any numbers at all.",
        category=EvidenceCategory.FACT,
        source_reference="https://example.com",
        observation_date=date(2026, 9, 1),
        metric_value=None,
    )

    with pytest.raises(ValueError, match="lacks a numeric metric_value"):
        ResearchTrendAnalyzer.analyze_trend(item)


# ── Provenance & Epistemic Separation Tests ────────────────────────────────────


def test_original_evidence_and_source_references_preserved():
    """Requirement 7: Original ResearchEvidenceItem instances and unique source refs are preserved."""
    ev1 = make_evidence(
        "Nanotechnology",
        date(2026, 9, 1),
        views=30000,
        source_ref="https://en.wikipedia.org/wiki/Nanotech_Day1",
    )
    ev2 = make_evidence(
        "Nanotechnology",
        date(2026, 9, 2),
        views=35000,
        source_ref="https://en.wikipedia.org/wiki/Nanotech_Day2",
    )

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    assert result.evidence_items[0] == ev1
    assert result.evidence_items[1] == ev2
    assert len(result.source_references) == 2
    assert "https://en.wikipedia.org/wiki/Nanotech_Day1" in result.source_references
    assert "https://en.wikipedia.org/wiki/Nanotech_Day2" in result.source_references


def test_no_commercial_claims_generated():
    """Requirement 8: Step 22 output must NEVER generate commercial or business claims."""
    ev1 = make_evidence("EdTech", date(2026, 9, 1), views=50000)
    ev2 = make_evidence("EdTech", date(2026, 9, 2), views=90000)

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    combined_text = (result.analysis + " " + result.fact_summary).lower()
    assert "market demand" not in combined_text
    assert "customer demand" not in combined_text
    assert "willingness to pay" not in combined_text
    assert "commercial viability" not in combined_text
    assert "profit" not in combined_text
    assert "revenue" not in combined_text
    assert "opportunity" not in combined_text
    assert "buyer" not in combined_text
    assert "customer" not in combined_text
    assert "monetization" not in combined_text
    assert "strong" not in combined_text
    assert "weak" not in combined_text
    assert "good" not in combined_text
    assert "bad" not in combined_text


def test_no_scores_rankings_or_confidence_values():
    """Requirement 9: Result contract must contain zero scores, ranks, or confidence values."""
    ev1 = make_evidence("Solar Energy", date(2026, 9, 1), views=10000)
    ev2 = make_evidence("Solar Energy", date(2026, 9, 2), views=20000)

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    assert not hasattr(result, "score")
    assert not hasattr(result, "opportunity_score")
    assert not hasattr(result, "trend_score")
    assert not hasattr(result, "confidence")
    assert not hasattr(result, "confidence_score")
    assert not hasattr(result, "rank")
    assert not hasattr(result, "ranking")
    assert not hasattr(result, "weight")


def test_topic_override_and_extraction():
    """Permits explicit topic specification or derives cleanly from statement quotes."""
    ev1 = make_evidence("Augmented Reality", date(2026, 9, 1), views=10000)
    ev2 = make_evidence("Augmented Reality", date(2026, 9, 2), views=15000)

    # Derived
    res_derived = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])
    assert res_derived.topic == "Augmented Reality"

    # Explicit override
    res_override = ResearchTrendAnalyzer.analyze_trend([ev1, ev2], topic="AR Hardware")
    assert res_override.topic == "AR Hardware"


# ── System Isolation & Guardrail Tests ─────────────────────────────────────────


def test_trend_analysis_causes_zero_database_writes(session: Session):
    """Requirement 10: Trend analysis must NOT create or alter records in SQLite."""
    ev1 = make_evidence("Biotechnology", date(2026, 9, 1), views=100000)
    ev2 = make_evidence("Biotechnology", date(2026, 9, 2), views=150000)

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])
    assert result is not None

    assert OpportunityRepository(session).list() == []
    assert CapitalRepository(session).get_transaction_history() == []
    assert ExperimentRepository(session).list() == []
    assert DecisionRepository(session).list() == []
    assert LearningRepository(session).list_for_opportunity(uuid4()) == []


def test_trend_analysis_causes_zero_financial_effects(session: Session):
    """Requirement 11: Capital and ledger must remain completely untouched."""
    ev = make_evidence("FinTech", date(2026, 9, 1), views=5000)
    _ = ResearchTrendAnalyzer.analyze_trend(ev)

    cap_repo = CapitalRepository(session)
    assert cap_repo.get_transaction_history() == []
    assert cap_repo.get_current_balance() == Decimal("0.00")


def test_trend_analysis_does_not_create_opportunity_or_experiment():
    """Requirements 12 & 13: Zero Opportunity and zero Experiment domain entities created."""
    ev1 = make_evidence("Aerospace", date(2026, 9, 1), views=10000)
    ev2 = make_evidence("Aerospace", date(2026, 9, 2), views=20000)

    result = ResearchTrendAnalyzer.analyze_trend([ev1, ev2])

    # Result is an analytical container, not an Opportunity or Experiment
    assert isinstance(result, ResearchTrendObservation)
    assert not hasattr(result, "phase")
    assert not hasattr(result, "tier")
    assert not hasattr(result, "allocated_budget")
    assert not hasattr(result, "experiment_id")
    assert not hasattr(result, "opportunity_id")
