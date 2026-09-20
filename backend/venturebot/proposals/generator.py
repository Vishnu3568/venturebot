"""Deterministic Opportunity -> Experiment Proposal generator."""

from __future__ import annotations

from decimal import Decimal

from venturebot.evaluation.evaluator import OpportunityEvaluator
from venturebot.evaluation.models import EvaluationStatus, OpportunityEvaluationResult
from venturebot.models.experiment import Channel, MonetizationMethod
from venturebot.models.opportunity import Opportunity, OpportunityCategory
from venturebot.proposals.models import ExperimentProposal, ProposalGenerationResult

STARTING_CAPITAL_CEILING = Decimal("1000.00")


class ProposalGenerator:
    """Transforms evaluated Opportunity records into structured Experiment proposals.
    
    Adheres strictly to the evaluation gate: only opportunities verified as
    READY_FOR_EXPERIMENT_DESIGN by OpportunityEvaluator will yield an eligible proposal.
    """

    @classmethod
    def generate(
        cls,
        opportunity: Opportunity,
        evaluation: OpportunityEvaluationResult | None = None,
        timeline_days: int | None = None,
    ) -> ProposalGenerationResult:
        """Generate a deterministic experiment proposal for an opportunity."""
        # 1. Run evaluation if not pre-supplied
        if evaluation is None:
            evaluation = OpportunityEvaluator.evaluate(opportunity)

        # 2. Evaluation Gate
        if evaluation.status != EvaluationStatus.READY_FOR_EXPERIMENT_DESIGN:
            return ProposalGenerationResult(
                is_eligible=False,
                evaluation_status=evaluation.status,
                proposal=None,
                rejection_reason=(
                    f"Opportunity '{opportunity.title}' is not ready for proposal design. "
                    f"Evaluation status is '{evaluation.status.value}'. "
                    f"Risk flags: {', '.join(evaluation.risk_flags) if evaluation.risk_flags else 'None'}"
                ),
            )

        # 3. Deterministic channel selection based on opportunity hints
        channel = cls._determine_channel(opportunity)

        # 4. Deterministic monetization method selection
        monetization = cls._determine_monetization(opportunity)

        # 5. Budget and spending cap formulation directly from opportunity estimates
        proposed_budget = opportunity.estimated_cost_min
        max_allowed_spend = (
            opportunity.estimated_cost_max
            if opportunity.estimated_cost_max >= proposed_budget
            else proposed_budget
        )
        if max_allowed_spend > STARTING_CAPITAL_CEILING:
            max_allowed_spend = STARTING_CAPITAL_CEILING

        revenue_target = (
            opportunity.estimated_revenue_min
            if opportunity.estimated_revenue_min > Decimal("0.00")
            else opportunity.estimated_revenue_max
        )
        estimated_margin = revenue_target - proposed_budget

        # 6. Formulate structured criteria & hypothesis without arbitrary assumptions
        hypothesis = (
            f"Offering '{opportunity.title}' to audience '{opportunity.audience}' "
            f"via channel '{channel.value}' will generate at least ₹{revenue_target:.2f} "
            f"with an allocated budget of ₹{proposed_budget:.2f}."
        )

        timeline_pilot_prefix = f"{timeline_days}-day " if timeline_days is not None else ""
        test_description = (
            f"Run a {timeline_pilot_prefix}controlled pilot testing audience demand for '{opportunity.title}' "
            f"monetized via {monetization.value} on {channel.value}."
        )

        success_criteria = (
            f"Achieve verified revenue >= ₹{revenue_target:.2f} "
            f"while keeping total actual spend <= ₹{max_allowed_spend:.2f}."
        )

        time_clause = f" within {timeline_days} days" if timeline_days is not None else ""
        failure_criteria = (
            f"Zero conversions or failure to observe required conversion signal{time_clause}, "
            f"or actual spend reaching maximum limit (₹{max_allowed_spend:.2f}) without generating revenue."
        )

        key_metrics = [
            "impressions",
            "clicks",
            "visitors",
            "conversions",
            "revenue",
            "actual_spend",
            "roas",
            "roi",
        ]

        evidence_for_decision = (
            f"Evaluate conversion signal and net margin (verified revenue - actual spend) "
            f"to inform decision (SCALE if profitable with positive demand signal, "
            f"ITERATE if demand signal exists but unit economics need adjustment, "
            f"or KILL if failure criteria are triggered)."
        )

        proposal_rationale = (
            f"Opportunity '{opportunity.title}' passed evaluation with {evaluation.completeness_ratio * 100:.0f}% "
            f"data completeness, positive margin expectation (min ₹{evaluation.estimated_margin_min:.2f}, "
            f"max ₹{evaluation.estimated_margin_max:.2f}), and verified source evidence."
        )

        proposal = ExperimentProposal(
            opportunity_id=opportunity.id,
            opportunity_title=opportunity.title,
            hypothesis=hypothesis,
            test_description=test_description,
            channel=channel,
            monetization_method=monetization,
            proposed_budget=proposed_budget,
            max_allowed_spend=max_allowed_spend,
            estimated_revenue_target=revenue_target,
            estimated_margin=estimated_margin,
            timeline_days=timeline_days,
            success_criteria=success_criteria,
            failure_criteria=failure_criteria,
            key_metrics_to_track=key_metrics,
            evidence_for_decision=evidence_for_decision,
            risks_and_constraints=list(evaluation.risk_flags),
            proposal_rationale=proposal_rationale,
        )

        return ProposalGenerationResult(
            is_eligible=True,
            evaluation_status=evaluation.status,
            proposal=proposal,
            rejection_reason=None,
        )

    @staticmethod
    def _determine_channel(opportunity: Opportunity) -> Channel:
        """Map opportunity platform dependency and distribution hints to a Channel enum."""
        platform = (opportunity.platform_dependency + " " + opportunity.distribution_difficulty).lower()
        if "instagram" in platform:
            return Channel.INSTAGRAM
        if "youtube" in platform:
            return Channel.YOUTUBE
        if "twitter" in platform or "x.com" in platform:
            return Channel.TWITTER_X
        if "linkedin" in platform:
            return Channel.LINKEDIN
        if "reddit" in platform:
            return Channel.REDDIT
        if "email" in platform or "newsletter" in platform:
            return Channel.EMAIL
        if "whatsapp" in platform:
            return Channel.WHATSAPP
        if "marketplace" in platform or "gumroad" in platform or "etsy" in platform:
            return Channel.MARKETPLACE
        return Channel.WEBSITE

    @staticmethod
    def _determine_monetization(opportunity: Opportunity) -> MonetizationMethod:
        """Map opportunity category and monetization notes to a MonetizationMethod enum."""
        notes = opportunity.monetization_notes.lower()
        if "subscription" in notes or "recurring" in notes or "saas" in notes:
            return MonetizationMethod.SUBSCRIPTION
        if "affiliate" in notes or opportunity.category == OpportunityCategory.AFFILIATE:
            return MonetizationMethod.AFFILIATE
        if "sponsorship" in notes or "sponsor" in notes:
            return MonetizationMethod.SPONSORSHIP
        if "lead" in notes:
            return MonetizationMethod.LEAD_GEN
        if "freelance" in notes or opportunity.category == OpportunityCategory.FREELANCE:
            return MonetizationMethod.FREELANCE
        if "ads" in notes or "ad revenue" in notes:
            return MonetizationMethod.ADS_REVENUE
        return MonetizationMethod.DIRECT_SALE
