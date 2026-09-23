"""Tests for Step 13 — Experiment Learning & Outcome Context Foundation.

Verifies:
1. Create valid learning record.
2. Reject empty summary.
3. Reject missing/empty key_learnings.
4. Preserve what_worked.
5. Preserve what_failed.
6. Preserve key_learnings.
7. Preserve future_hypotheses.
8. Valid learning with no decision_id.
9. Valid learning with decision_id.
10. Reject nonexistent experiment.
11. Reject nonexistent opportunity.
12. Reject nonexistent decision.
13. Multiple learning records can belong to one experiment.
14. list_for_experiment() returns the records.
15. list_for_opportunity() returns the records.
16. get_latest_for_experiment() returns the correct latest record.
17. Creating learning creates zero capital transactions.
18. Creating learning does not alter capital balance.
19. Creating learning creates zero decisions.
20. Creating learning does not change experiment status.
21. Learning records are strictly append-only (no update/delete methods).
"""

from collections.abc import Generator
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.decision import DecisionRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.learning import LearningRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.decision.service import ExperimentDecisionService
from venturebot.learning.models import ExperimentLearning
from venturebot.learning.service import ExperimentLearningService
from venturebot.models.decision import Decision, DecisionOutcome
from venturebot.models.experiment import Channel, Experiment, ExperimentStatus, MonetizationMethod
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Isolated SQLite in-memory database session."""
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture
def sample_opportunity(session: Session) -> Opportunity:
    """Persisted opportunity fixture."""
    repo = OpportunityRepository(session)
    return repo.create(
        Opportunity(
            title="Developer Resume Formatter",
            description="Tool to format developer resumes for technical recruiters.",
            category=OpportunityCategory.PRODUCT,
            status=OpportunityStatus.APPROVED,
            source="Developer surveys",
            evidence_notes="Verified interest from 10 developers",
            estimated_revenue_min=Decimal("500.00"),
            estimated_revenue_max=Decimal("1500.00"),
            estimated_cost_min=Decimal("50.00"),
            estimated_cost_max=Decimal("100.00"),
            confidence=0.8,
        )
    )


@pytest.fixture
def sample_experiment(session: Session, sample_opportunity: Opportunity) -> Experiment:
    """Persisted running experiment fixture."""
    repo = ExperimentRepository(session)
    return repo.create(
        Experiment(
            opportunity_id=sample_opportunity.id,
            hypothesis="Offering ATS resume formatting for ₹199 will yield >= 5% conversion.",
            objective="Validate paid conversions from developer outreach",
            channel=Channel.LINKEDIN,
            monetization_method=MonetizationMethod.DIRECT_SALE,
            allocated_budget=Decimal("50.00"),
            max_allowed_spend=Decimal("100.00"),
            success_criteria="At least 3 sales",
            failure_criteria="Zero sales after 50 targeted contacts",
            status=ExperimentStatus.RUNNING,
        )
    )


@pytest.fixture
def sample_decision(session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity) -> Decision:
    """Persisted explicit decision fixture."""
    return ExperimentDecisionService.record_decision(
        session,
        Decision(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            outcome=DecisionOutcome.SCALE,
            reason="Converted 4 out of 30 leads with positive feedback; unit economics viable",
            evidence_summary="4 sales, ₹796 revenue, ₹35 spend",
            confidence=0.85,
        ),
    )


# ── Tests 1 to 9: Contract & Validation ──────────────────────────────────────────

def test_create_valid_learning_record(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 1: Successfully create and persist a valid ExperimentLearning record."""
    learning = ExperimentLearning(
        experiment_id=sample_experiment.id,
        opportunity_id=sample_opportunity.id,
        summary="LinkedIn direct outreach achieved positive unit economics.",
        what_worked=["Personalized connection note mentioning tech stack", "Pricing at ₹199"],
        what_failed=["Generic follow-up templates had 0% reply rate"],
        key_learnings=["Technical candidates respond when formatting problem is demonstrated with examples"],
        future_hypotheses=["Offering automated PDF preview will increase conversion rate to 8%"],
    )

    persisted = ExperimentLearningService.record_learning(session, learning)
    assert persisted.id == learning.id
    assert persisted.experiment_id == sample_experiment.id
    assert persisted.opportunity_id == sample_opportunity.id
    assert persisted.decision_id is None
    assert persisted.summary == "LinkedIn direct outreach achieved positive unit economics."
    assert len(persisted.what_worked) == 2
    assert len(persisted.what_failed) == 1
    assert len(persisted.key_learnings) == 1
    assert len(persisted.future_hypotheses) == 1


def test_reject_empty_summary(sample_experiment: Experiment, sample_opportunity: Opportunity):
    """Test 2: Learning requires a non-empty summary."""
    with pytest.raises(ValidationError) as exc:
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="   ",
            key_learnings=["Some learning"],
        )
    assert "Learning summary must not be empty" in str(exc.value)


def test_reject_missing_empty_key_learnings(sample_experiment: Experiment, sample_opportunity: Opportunity):
    """Test 3: Learning requires at least one non-empty key learning."""
    with pytest.raises(ValidationError) as exc_empty:
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Valid summary",
            key_learnings=[],
        )
    assert "At least one non-empty key learning is required" in str(exc_empty.value)

    with pytest.raises(ValidationError) as exc_whitespace:
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Valid summary",
            key_learnings=["   ", ""],
        )
    assert "At least one non-empty key learning is required" in str(exc_whitespace.value)


def test_preserve_what_worked(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 4: Preserves what_worked list exactly as explicitly supplied."""
    items = ["Direct message outreach", "Showing before/after resume teaser"]
    learning = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Tested acquisition channel",
            what_worked=items,
            key_learnings=["Demos convert better than text"],
        ),
    )
    assert learning.what_worked == items


def test_preserve_what_failed(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 5: Preserves what_failed list exactly as explicitly supplied."""
    items = ["Email cold outreach", "Weekend posting"]
    learning = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Tested acquisition channel",
            what_failed=items,
            key_learnings=["Cold email spam filters blocked messages"],
        ),
    )
    assert learning.what_failed == items


def test_preserve_key_learnings(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 6: Preserves key_learnings list exactly as explicitly supplied."""
    items = ["Developers prefer self-service checkout", "Pricing resistance at > ₹300"]
    learning = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Pricing and UX test",
            key_learnings=items,
        ),
    )
    assert learning.key_learnings == items


def test_preserve_future_hypotheses(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 7: Preserves future_hypotheses list exactly as explicitly supplied."""
    items = ["Testing GitHub sponsors integration will reduce checkout friction"]
    learning = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Channel friction test",
            key_learnings=["Payment friction was the primary drop-off cause"],
            future_hypotheses=items,
        ),
    )
    assert learning.future_hypotheses == items


def test_valid_learning_with_no_decision_id(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 8: Valid learning can be recorded without a decision_id."""
    learning = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            decision_id=None,
            summary="Intermediate observation",
            key_learnings=["Initial cohort response is positive"],
        ),
    )
    assert learning.decision_id is None
    fetched = ExperimentLearningService.get_learning(session, learning.id)
    assert fetched is not None
    assert fetched.decision_id is None


def test_valid_learning_with_decision_id(
    session: Session,
    sample_experiment: Experiment,
    sample_opportunity: Opportunity,
    sample_decision: Decision,
):
    """Test 9: Valid learning linked to an existing Decision record."""
    learning = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            decision_id=sample_decision.id,
            summary="Post-scale evaluation learning",
            key_learnings=["Scale decision justified by positive pilot economics"],
        ),
    )
    assert learning.decision_id == sample_decision.id
    fetched = ExperimentLearningService.get_learning(session, learning.id)
    assert fetched is not None
    assert fetched.decision_id == sample_decision.id


# ── Tests 10 to 12: Referential Integrity ───────────────────────────────────────

def test_reject_nonexistent_experiment(session: Session, sample_opportunity: Opportunity):
    """Test 10: Reject learning with nonexistent experiment_id."""
    fake_exp_id = uuid4()
    with pytest.raises(ValueError) as exc:
        ExperimentLearningService.record_learning(
            session,
            ExperimentLearning(
                experiment_id=fake_exp_id,
                opportunity_id=sample_opportunity.id,
                summary="Invalid experiment test",
                key_learnings=["Key learning"],
            ),
        )
    assert f"Experiment '{fake_exp_id}' does not exist" in str(exc.value)


def test_reject_nonexistent_opportunity(session: Session, sample_experiment: Experiment):
    """Test 11: Reject learning with nonexistent opportunity_id."""
    fake_opp_id = uuid4()
    with pytest.raises(ValueError) as exc:
        ExperimentLearningService.record_learning(
            session,
            ExperimentLearning(
                experiment_id=sample_experiment.id,
                opportunity_id=fake_opp_id,
                summary="Invalid opportunity test",
                key_learnings=["Key learning"],
            ),
        )
    assert f"Opportunity '{fake_opp_id}' does not exist" in str(exc.value)


def test_reject_nonexistent_decision(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 12: Reject learning with nonexistent decision_id when provided."""
    fake_dec_id = uuid4()
    with pytest.raises(ValueError) as exc:
        ExperimentLearningService.record_learning(
            session,
            ExperimentLearning(
                experiment_id=sample_experiment.id,
                opportunity_id=sample_opportunity.id,
                decision_id=fake_dec_id,
                summary="Invalid decision test",
                key_learnings=["Key learning"],
            ),
        )
    assert f"Decision '{fake_dec_id}' does not exist" in str(exc.value)


# ── Tests 13 to 16: Cardinality and Queries ─────────────────────────────────────

def test_multiple_learning_records_and_list_queries(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 13, 14, 15: Multiple learning records can belong to one experiment and opportunity."""
    time_base = datetime.now(timezone.utc)

    rec1 = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="First milestone learning",
            key_learnings=["First insight on customer acquisition"],
            recorded_at=time_base - timedelta(hours=2),
        ),
    )
    rec2 = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Second milestone learning",
            key_learnings=["Second insight on pricing resistance"],
            recorded_at=time_base - timedelta(hours=1),
        ),
    )
    rec3 = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Final experiment retrospective",
            key_learnings=["Final durable lesson"],
            recorded_at=time_base,
        ),
    )

    # list_for_experiment returns records in reverse chronological order
    exp_learnings = ExperimentLearningService.list_learnings_for_experiment(
        session, sample_experiment.id
    )
    assert len(exp_learnings) == 3
    assert [l.id for l in exp_learnings] == [rec3.id, rec2.id, rec1.id]

    # list_for_opportunity returns records for the opportunity
    opp_learnings = ExperimentLearningService.list_learnings_for_opportunity(
        session, sample_opportunity.id
    )
    assert len(opp_learnings) == 3
    assert [l.id for l in opp_learnings] == [rec3.id, rec2.id, rec1.id]


def test_get_latest_for_experiment(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 16: get_latest_for_experiment returns the most recently recorded record."""
    # When no learning exists
    assert ExperimentLearningService.get_latest_learning_for_experiment(session, sample_experiment.id) is None

    time_base = datetime.now(timezone.utc)
    ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Older learning",
            key_learnings=["Older lesson"],
            recorded_at=time_base - timedelta(hours=1),
        ),
    )
    latest_rec = ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Newest learning",
            key_learnings=["Newest lesson"],
            recorded_at=time_base,
        ),
    )

    latest = ExperimentLearningService.get_latest_learning_for_experiment(session, sample_experiment.id)
    assert latest is not None
    assert latest.id == latest_rec.id
    assert latest.summary == "Newest learning"


# ── Tests 17 to 20: Side-Effect and Isolation Verification ────────────────────────

def test_learning_causes_zero_financial_side_effects(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 17 & 18: Recording learning creates zero capital transactions and does not alter balance."""
    cap_repo = CapitalRepository(session)
    cap_repo.initialize_starting_capital()

    summary_before = cap_repo.get_financial_summary()
    tx_count_before = len(cap_repo.get_transaction_history())

    ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Retrospective learning note",
            what_worked=["Targeted audience message"],
            what_failed=["Generic email"],
            key_learnings=["Audience specificity drives engagement"],
        ),
    )

    summary_after = cap_repo.get_financial_summary()
    tx_count_after = len(cap_repo.get_transaction_history())

    assert tx_count_after == tx_count_before
    assert summary_after.current_balance == summary_before.current_balance
    assert summary_after.total_cost == summary_before.total_cost
    assert summary_after.total_allocated == summary_before.total_allocated
    assert summary_after.available_unallocated == summary_before.available_unallocated


def test_learning_causes_zero_decision_side_effects(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 19: Recording learning creates zero decision records."""
    dec_repo = DecisionRepository(session)
    dec_count_before = len(dec_repo.list(experiment_id=sample_experiment.id))

    ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Retrospective observation without decision",
            key_learnings=["Durable insight"],
        ),
    )

    dec_count_after = len(dec_repo.list(experiment_id=sample_experiment.id))
    assert dec_count_after == dec_count_before


def test_learning_does_not_mutate_experiment_or_opportunity_status(
    session: Session, sample_experiment: Experiment, sample_opportunity: Opportunity
):
    """Test 20: Recording learning does not change experiment or opportunity status."""
    exp_status_before = sample_experiment.status
    opp_status_before = sample_opportunity.status

    ExperimentLearningService.record_learning(
        session,
        ExperimentLearning(
            experiment_id=sample_experiment.id,
            opportunity_id=sample_opportunity.id,
            summary="Retrospective analysis note",
            key_learnings=["Insight"],
        ),
    )

    exp_after = ExperimentRepository(session).get(sample_experiment.id)
    opp_after = OpportunityRepository(session).get(sample_opportunity.id)

    assert exp_after is not None
    assert exp_after.status == exp_status_before
    assert opp_after is not None
    assert opp_after.status == opp_status_before


def test_learning_records_are_append_only():
    """Verify LearningRepository and ExperimentLearningService expose NO update or delete methods."""
    for cls in (LearningRepository, ExperimentLearningService):
        assert not hasattr(cls, "update"), f"{cls.__name__} must not have 'update' method"
        assert not hasattr(cls, "delete"), f"{cls.__name__} must not have 'delete' method"
        assert not hasattr(cls, "remove"), f"{cls.__name__} must not have 'remove' method"
