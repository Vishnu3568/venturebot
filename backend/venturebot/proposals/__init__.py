"""Proposals package for VentureBot Opportunity -> Experiment planning."""

from venturebot.proposals.generator import ProposalGenerator
from venturebot.proposals.models import ExperimentProposal, ProposalGenerationResult

__all__ = [
    "ExperimentProposal",
    "ProposalGenerationResult",
    "ProposalGenerator",
]
