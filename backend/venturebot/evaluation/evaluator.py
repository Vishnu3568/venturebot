"""Deterministic Opportunity evaluator based on locked architectural requirements."""

from __future__ import annotations

from decimal import Decimal

from venturebot.evaluation.models import EvaluationStatus, OpportunityEvaluationResult
from venturebot.models.opportunity import Opportunity

STARTING_CAPITAL_CEILING = Decimal("1000.00")

EVALUATED_DIMENSIONS = [
    ("source", "Source / Reference"),
    ("evidence_notes", "Evidence Notes"),
    ("audience", "Audience"),
    ("trend_strength", "Trend Strength"),
    ("growth_indicators", "Growth Indicators"),
    ("competition_level", "Competition Level"),
    ("monetization_notes", "Monetization Notes"),
    ("production_difficulty", "Production Difficulty"),
    ("distribution_difficulty", "Distribution Difficulty"),
    ("automation_potential", "Automation Potential"),
    ("platform_dependency", "Platform Dependency"),
    ("regulatory_notes", "Regulatory / Risk Notes"),
]


class OpportunityEvaluator:
    """Evaluates an Opportunity against evidence, economic, and feasibility criteria.
    
    Adheres strictly to VENTUREBOT_ARCHITECTURE.md Section 8 and Section 17:
    - Zero speculative weighting or arbitrary scoring formulas.
    - Deterministic evaluation: identical inputs always yield identical results.
    - Zero mutations to database, financial ledger, or input models.
    """

    @staticmethod
    def evaluate(opportunity: Opportunity) -> OpportunityEvaluationResult:
        """Perform a deterministic evaluation on an Opportunity instance."""
        missing_dimensions: list[str] = []
        provided_count = 0
        total_count = len(EVALUATED_DIMENSIONS)

        for attr, label in EVALUATED_DIMENSIONS:
            val = getattr(opportunity, attr, "")
            if isinstance(val, str) and val.strip():
                provided_count += 1
            else:
                missing_dimensions.append(label)

        completeness_ratio = round(provided_count / total_count, 4) if total_count > 0 else 0.0
        has_source_evidence = bool(opportunity.source.strip() or opportunity.evidence_notes.strip())

        # Economics analysis
        has_economic_estimates = bool(
            opportunity.estimated_revenue_max > Decimal("0.00")
            or opportunity.estimated_cost_max > Decimal("0.00")
        )
        estimated_margin_min = opportunity.estimated_revenue_min - opportunity.estimated_cost_max
        estimated_margin_max = opportunity.estimated_revenue_max - opportunity.estimated_cost_min

        # Capital risk check against starting capital ₹1,000
        exceeds_starting_capital_risk = opportunity.estimated_cost_min > STARTING_CAPITAL_CEILING

        # Factual risk flags
        risk_flags: list[str] = []
        if not has_source_evidence:
            risk_flags.append("Missing verified source or evidence references")

        if exceeds_starting_capital_risk:
            risk_flags.append(
                f"Estimated minimum cost (₹{opportunity.estimated_cost_min}) exceeds available starting capital (₹{STARTING_CAPITAL_CEILING})"
            )

        if opportunity.platform_dependency.strip():
            risk_flags.append(f"Platform lock-in risk noted: {opportunity.platform_dependency.strip()}")

        if opportunity.regulatory_notes.strip():
            risk_flags.append(f"Regulatory/policy risk noted: {opportunity.regulatory_notes.strip()}")

        if opportunity.competition_level.strip().lower() in ["high", "very high", "severe", "saturated"]:
            risk_flags.append(f"High competition level flagged: {opportunity.competition_level.strip()}")

        # Status determination
        if exceeds_starting_capital_risk:
            status = EvaluationStatus.EXCEEDS_CAPITAL_LIMIT
        elif not has_source_evidence:
            status = EvaluationStatus.NEEDS_EVIDENCE
        elif not has_economic_estimates:
            status = EvaluationStatus.INSUFFICIENT_ECONOMICS
        elif not opportunity.audience.strip() or not opportunity.monetization_notes.strip():
            status = EvaluationStatus.INCOMPLETE
        else:
            status = EvaluationStatus.READY_FOR_EXPERIMENT_DESIGN

        return OpportunityEvaluationResult(
            opportunity_id=opportunity.id,
            status=status,
            has_source_evidence=has_source_evidence,
            provided_dimensions_count=provided_count,
            total_dimensions_count=total_count,
            completeness_ratio=completeness_ratio,
            missing_dimensions=missing_dimensions,
            has_economic_estimates=has_economic_estimates,
            estimated_margin_min=estimated_margin_min,
            estimated_margin_max=estimated_margin_max,
            exceeds_starting_capital_risk=exceeds_starting_capital_risk,
            risk_flags=risk_flags,
        )
