"""Unit tests for Opportunity -> Experiment Proposal Foundation (Step 6).

Verifies:
- Valid Opportunity produces a structured ExperimentProposal
- Proposal references the correct Opportunity ID and Title
- Proposal contains hypothesis, test description, channel, and monetization method
- Proposed budget and max_allowed_spend are calculated strictly from opportunity data without arbitrary fallbacks
- No arbitrary 14-day default timeline exists (timeline is optional/None unless explicitly supplied)
- No arbitrary ₹50 budget fallback exists
- No arbitrary ROI or percentage failure thresholds are used
- Missing economics are blocked by the evaluation gate
- Deterministic output across multiple runs
- Evaluation gate blocks proposals for opportunities needing evidence, incomplete data, or exceeding capital
- Zero capital transactions and zero actual spending caused by proposal generation
- Clean conversion to canonical Experiment in DRAFT status
- Total isolation from financial ledger
"""

from decimal import Decimal

import pytest

from venturebot.database.connection import get_engine, get_session_factory, init_db
from venturebot.database.repositories.capital import CapitalRepository
from venturebot.database.repositories.experiment import ExperimentRepository
from venturebot.database.repositories.opportunity import OpportunityRepository
from venturebot.evaluation.models import EvaluationStatus
from venturebot.models.experiment import Channel, ExperimentStatus, MonetizationMethod
from venturebot.models.opportunity import Opportunity, OpportunityCategory, OpportunityStatus
from venturebot.proposals.generator import ProposalGenerator


@pytest.fixture
def sample_valid_opportunity() -> Opportunity:
    return Opportunity(
        title="Resume Optimizer for Developers",
        description="CLI tool to parse and format resumes for ATS tracking.",
        category=OpportunityCategory.PRODUCT,
        status=OpportunityStatus.DISCOVERED,
        source="Reddit /r/cscareerquestions survey 2026",
        evidence_notes="High demand for developer ATS formatting.",
        audience="Junior and Mid-level developers in India",
        trend_strength="Rising hiring volume in Q3",
        growth_indicators="ATS search queries up 25%",
        competition_level="Moderate",
        monetization_notes="Direct license sale at ₹299",
        production_difficulty="Low",
        distribution_difficulty="Direct via LinkedIn outreach",
        automation_potential="High",
        platform_dependency="LinkedIn and GitHub",
        regulatory_notes="None",
        estimated_revenue_min=Decimal("600.00"),
        estimated_revenue_max=Decimal("1500.00"),
        estimated_cost_min=Decimal("80.00"),
        estimated_cost_max=Decimal("150.00"),
        confidence=0.85,
    )


def test_valid_opportunity_generates_experiment_proposal(sample_valid_opportunity: Opportunity):
    """Valid opportunity yields an eligible, structured ExperimentProposal without invented defaults."""
    result = ProposalGenerator.generate(sample_valid_opportunity)

    assert result.is_eligible is True
    assert result.evaluation_status == EvaluationStatus.READY_FOR_EXPERIMENT_DESIGN
    assert result.proposal is not None

    prop = result.proposal
    assert prop.opportunity_id == sample_valid_opportunity.id
    assert prop.opportunity_title == sample_valid_opportunity.title
    assert "Resume Optimizer for Developers" in prop.hypothesis
    assert prop.channel == Channel.LINKEDIN
    assert prop.monetization_method == MonetizationMethod.DIRECT_SALE
    assert prop.proposed_budget == Decimal("80.00")
    assert prop.max_allowed_spend == Decimal("150.00")
    assert prop.estimated_revenue_target == Decimal("600.00")
    assert prop.estimated_margin == Decimal("520.00")  # 600 - 80
    assert prop.timeline_days is None  # No invented default duration
    assert len(prop.success_criteria) > 0
    assert len(prop.failure_criteria) > 0
    assert "conversions" in prop.key_metrics_to_track
    assert len(prop.proposal_rationale) > 0


def test_no_arbitrary_timeline_default_and_explicit_timeline_support(sample_valid_opportunity: Opportunity):
    """Proposal timeline is None by default and only set when explicitly supplied."""
    # 1. Default generation has no invented 14-day timeline
    res_default = ProposalGenerator.generate(sample_valid_opportunity)
    assert res_default.proposal is not None
    assert res_default.proposal.timeline_days is None
    assert "14-day" not in res_default.proposal.test_description
    assert "14 days" not in res_default.proposal.failure_criteria

    # 2. Explicit timeline is preserved when provided
    res_explicit = ProposalGenerator.generate(sample_valid_opportunity, timeline_days=7)
    assert res_explicit.proposal is not None
    assert res_explicit.proposal.timeline_days == 7
    assert "7-day" in res_explicit.proposal.test_description
    assert "7 days" in res_explicit.proposal.failure_criteria


def test_no_arbitrary_budget_fallback(sample_valid_opportunity: Opportunity):
    """Proposed budget and ceiling derive strictly from opportunity inputs with zero arbitrary fallbacks."""
    # Explicit zero-cost opportunity with revenue
    zero_cost_opp = sample_valid_opportunity.model_copy(
        update={
            "estimated_cost_min": Decimal("0.00"),
            "estimated_cost_max": Decimal("0.00"),
        }
    )
    result = ProposalGenerator.generate(zero_cost_opp)
    assert result.is_eligible is True
    assert result.proposal is not None
    assert result.proposal.proposed_budget == Decimal("0.00")  # NOT ₹50
    assert result.proposal.max_allowed_spend == Decimal("0.00")  # NOT ₹50


def test_no_arbitrary_roi_or_percentage_failure_thresholds(sample_valid_opportunity: Opportunity):
    """Criteria and decision evidence must not contain invented ROI or percentage cutoffs."""
    result = ProposalGenerator.generate(sample_valid_opportunity)
    assert result.proposal is not None

    # No arbitrary numerical ROI threshold
    assert "ROI >" not in result.proposal.evidence_for_decision
    assert "0.5" not in result.proposal.evidence_for_decision

    # No arbitrary 20% failure threshold
    assert "0.20" not in result.proposal.failure_criteria
    assert "20%" not in result.proposal.failure_criteria


def test_proposal_deterministic_output(sample_valid_opportunity: Opportunity):
    """Proposal generation is 100% deterministic given identical inputs."""
    res1 = ProposalGenerator.generate(sample_valid_opportunity)
    res2 = ProposalGenerator.generate(sample_valid_opportunity)

    assert res1.is_eligible == res2.is_eligible
    assert res1.proposal is not None and res2.proposal is not None
    assert res1.proposal.hypothesis == res2.proposal.hypothesis
    assert res1.proposal.proposed_budget == res2.proposal.proposed_budget
    assert res1.proposal.max_allowed_spend == res2.proposal.max_allowed_spend
    assert res1.proposal.success_criteria == res2.proposal.success_criteria


def test_evaluation_gate_blocks_missing_evidence():
    """Opportunity lacking verified evidence is rejected by the proposal gate."""
    unverified_opp = Opportunity(
        title="Unverified Idea",
        description="No source or research",
        category=OpportunityCategory.PRODUCT,
        audience="Public",
        monetization_notes="Direct sale",
        estimated_revenue_max=Decimal("1000.00"),
        estimated_cost_max=Decimal("100.00"),
    )
    result = ProposalGenerator.generate(unverified_opp)

    assert result.is_eligible is False
    assert result.proposal is None
    assert result.evaluation_status == EvaluationStatus.NEEDS_EVIDENCE
    assert "not ready for proposal design" in (result.rejection_reason or "")


def test_evaluation_gate_blocks_insufficient_economics():
    """Opportunity with zero revenue and cost estimates is rejected by the evaluation gate."""
    no_econ_opp = Opportunity(
        title="No Economics",
        description="Lacks financial estimates",
        category=OpportunityCategory.CONTENT,
        source="Verified Survey 2026",
        evidence_notes="Verified trend",
        audience="Students",
        monetization_notes="Affiliate",
        estimated_revenue_min=Decimal("0.00"),
        estimated_revenue_max=Decimal("0.00"),
        estimated_cost_min=Decimal("0.00"),
        estimated_cost_max=Decimal("0.00"),
    )
    result = ProposalGenerator.generate(no_econ_opp)

    assert result.is_eligible is False
    assert result.proposal is None
    assert result.evaluation_status == EvaluationStatus.INSUFFICIENT_ECONOMICS


def test_evaluation_gate_blocks_capital_limit_exceeded():
    """Opportunity with costs exceeding starting capital is rejected by proposal gate."""
    expensive_opp = Opportunity(
        title="High cost setup",
        description="Requires heavy infrastructure",
        category=OpportunityCategory.PRODUCT,
        source="Tech spec report",
        audience="Enterprises",
        monetization_notes="SaaS subscription",
        estimated_cost_min=Decimal("2000.00"),  # Exceeds ₹1,000 pool
        estimated_cost_max=Decimal("4000.00"),
        estimated_revenue_max=Decimal("8000.00"),
    )
    result = ProposalGenerator.generate(expensive_opp)

    assert result.is_eligible is False
    assert result.proposal is None
    assert result.evaluation_status == EvaluationStatus.EXCEEDS_CAPITAL_LIMIT
    assert "exceeds_capital_limit" in (result.rejection_reason or "")


def test_evaluation_gate_blocks_incomplete_opportunity():
    """Opportunity missing essential feasibility details is rejected."""
    incomplete_opp = Opportunity(
        title="Incomplete",
        description="Missing audience and monetization",
        category=OpportunityCategory.CONTENT,
        source="Google Search trend",
        estimated_revenue_max=Decimal("500.00"),
        estimated_cost_max=Decimal("50.00"),
    )
    result = ProposalGenerator.generate(incomplete_opp)

    assert result.is_eligible is False
    assert result.proposal is None
    assert result.evaluation_status == EvaluationStatus.INCOMPLETE


def test_proposal_to_experiment_conversion(sample_valid_opportunity: Opportunity):
    """ExperimentProposal converts into a valid Experiment contract with DRAFT status and zero actual spend."""
    result = ProposalGenerator.generate(sample_valid_opportunity)
    assert result.proposal is not None

    exp = result.proposal.to_experiment()
    assert exp.opportunity_id == sample_valid_opportunity.id
    assert exp.status == ExperimentStatus.DRAFT
    assert exp.allocated_budget == result.proposal.proposed_budget
    assert exp.max_allowed_spend == result.proposal.max_allowed_spend
    assert exp.actual_spend == Decimal("0.00")
    assert exp.hypothesis == result.proposal.hypothesis


def test_proposal_generation_causes_zero_capital_transactions_and_zero_spend(sample_valid_opportunity: Opportunity):
    """
    Critical financial safety check:
    Generating proposals must NOT deduct money, disburse funds, or write to the capital ledger.
    """
    engine = get_engine("sqlite:///:memory:")
    init_db(engine)
    session_factory = get_session_factory(engine)

    with session_factory() as session:
        cap_repo = CapitalRepository(session)
        opp_repo = OpportunityRepository(session)
        exp_repo = ExperimentRepository(session)

        # Initialize starting capital at ₹1,000
        cap_repo.initialize_starting_capital()
        initial_balance = cap_repo.get_current_balance()
        initial_tx_count = len(cap_repo.get_transaction_history())

        assert initial_balance == Decimal("1000.00")
        assert initial_tx_count == 1

        # Save opportunity
        saved_opp = opp_repo.create(sample_valid_opportunity)

        # Generate proposal
        gen_result = ProposalGenerator.generate(saved_opp)
        assert gen_result.is_eligible is True
        assert gen_result.proposal is not None

        # Convert to draft experiment and persist
        draft_exp = gen_result.proposal.to_experiment()
        saved_exp = exp_repo.create(draft_exp)
        assert saved_exp.status == ExperimentStatus.DRAFT
        assert saved_exp.actual_spend == Decimal("0.00")

        # Verify ledger is 100% untouched
        final_balance = cap_repo.get_current_balance()
        final_tx_count = len(cap_repo.get_transaction_history())

        assert final_balance == Decimal("1000.00")
        assert final_tx_count == 1
        assert final_balance == initial_balance
