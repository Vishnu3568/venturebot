"""venturebot.opportunity — Opportunity Intelligence and Ingestion Foundation."""

from venturebot.opportunity.candidate import OpportunityCandidateGenerator
from venturebot.opportunity.collection import ResearchCollectionService
from venturebot.opportunity.ingestion import OpportunityIngestionService
from venturebot.opportunity.models import (
    HumanOpportunitySpecification,
    OpportunityCandidate,
    OpportunityExperimentSummary,
    OpportunityIngestionPayload,
    OpportunityIntelligenceContext,
    ResearchCollectionResult,
    ResearchEvidenceItem,
    ResearchTrendObservation,
    TrendStatus,
)
from venturebot.opportunity.service import OpportunityIntelligenceService
from venturebot.opportunity.trend import ResearchTrendAnalyzer
from venturebot.opportunity.wikimedia import WikimediaPageviewsAdapter

__all__ = [
    "HumanOpportunitySpecification",
    "OpportunityCandidate",
    "OpportunityCandidateGenerator",
    "OpportunityExperimentSummary",
    "OpportunityIngestionPayload",
    "OpportunityIngestionService",
    "OpportunityIntelligenceContext",
    "OpportunityIntelligenceService",
    "ResearchCollectionResult",
    "ResearchCollectionService",
    "ResearchEvidenceItem",
    "ResearchTrendAnalyzer",
    "ResearchTrendObservation",
    "TrendStatus",
    "WikimediaPageviewsAdapter",
]

