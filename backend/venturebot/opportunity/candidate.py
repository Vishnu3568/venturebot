"""Opportunity Candidate Generation for VentureBot (Step 21).

Provides deterministic transformation of discrete research evidence into
human-reviewable OpportunityCandidate hypotheses without making automated
business claims, scoring opportunities, or writing to database persistence.
"""

from __future__ import annotations

from typing import Sequence

from venturebot.models.evidence import EvidenceCategory
from venturebot.opportunity.models import (
    OpportunityCandidate,
    ResearchCollectionResult,
    ResearchEvidenceItem,
    ResearchTrendObservation,
)


class OpportunityCandidateGenerator:
    """Deterministic generator converting research evidence into opportunity candidate hypotheses."""

    @staticmethod
    def extract_topic_from_statement(statement: str) -> str:
        """Deterministically extract the subject topic from an evidence statement."""
        clean = statement.strip()
        if not clean:
            return "Unspecified Topic"

        # Check for quoted topic pattern: e.g. Wikipedia article 'Artificial intelligence' recorded...
        first_quote = clean.find("'")
        if first_quote != -1:
            second_quote = clean.find("'", first_quote + 1)
            if second_quote != -1:
                extracted = clean[first_quote + 1 : second_quote].strip()
                if extracted:
                    return extracted

        # Fallback: full statement if short or clean
        return clean

    @classmethod
    def generate_candidate_from_evidence(
        cls,
        evidence: ResearchEvidenceItem | Sequence[ResearchEvidenceItem],
        topic: str | None = None,
    ) -> OpportunityCandidate:
        """Convert one or more ResearchEvidenceItem instances into an OpportunityCandidate hypothesis.

        Enforces:
        - At least one evidence item required.
        - Preserves original evidence items and source references without modification.
        - Keeps FACT evidence separate from derived OBSERVATION, INFERENCE, and HYPOTHESIS.
        - Explicitly documents UNKNOWNS (commercial intent, monetization, customer segment, unit economics).
        - Zero scoring, zero ranking, zero automatic opportunity persistence.
        """
        if isinstance(evidence, str):
            raise ValueError("evidence must be a ResearchEvidenceItem or a sequence of ResearchEvidenceItem, not str.")
        elif isinstance(evidence, ResearchEvidenceItem):
            items = [evidence]
        elif isinstance(evidence, Sequence):
            items = list(evidence)
        else:
            raise ValueError("evidence must be a ResearchEvidenceItem or a sequence of ResearchEvidenceItem.")

        if not items:
            raise ValueError("At least one ResearchEvidenceItem is required to generate an OpportunityCandidate.")

        for item in items:
            if not isinstance(item, ResearchEvidenceItem):
                raise ValueError(f"Expected ResearchEvidenceItem, got {type(item).__name__}.")

        first_item = items[0]
        derived_topic = (
            topic.strip()
            if topic and topic.strip()
            else cls.extract_topic_from_statement(first_item.statement)
        )

        # Collect unique source references
        source_refs: list[str] = []
        for it in items:
            if it.source_reference and it.source_reference not in source_refs:
                source_refs.append(it.source_reference)

        # Compile observation summary
        if len(items) == 1:
            observation_text = f"Empirical observation recorded: {first_item.statement}"
        else:
            observation_text = (
                f"Multiple empirical observations ({len(items)} items) recorded around topic '{derived_topic}'."
            )

        # Deterministic cognitive separation
        if len(items) == 1:
            inference_text = (
                f"Supplied evidence indicates that topic '{derived_topic}' received measurable public attention "
                f"in the recorded observation, which may warrant exploratory opportunity investigation."
            )
        else:
            inference_text = (
                f"Supplied evidence indicates that topic '{derived_topic}' received measurable public attention "
                f"across {len(items)} recorded observations, which may warrant exploratory opportunity investigation."
            )

        hypothesis_text = (
            f"A viable product, content, or service opportunity addressing needs related to '{derived_topic}' "
            f"may exist, subject to commercial validation."
        )

        unknowns = [
            f"Commercial intent and willingness to pay around '{derived_topic}' are unverified.",
            f"Specific customer segments and problem definitions for '{derived_topic}' are unverified.",
            f"Viable monetization models (product, content, service, affiliate) for '{derived_topic}' are unverified.",
            f"Distribution channels, competition level, and unit economics for '{derived_topic}' are unverified.",
        ]

        return OpportunityCandidate(
            title=f"Opportunity Candidate: {derived_topic}",
            topic=derived_topic,
            observation=observation_text,
            inference=inference_text,
            hypothesis=hypothesis_text,
            unknowns=unknowns,
            evidence_items=items,
            source_references=source_refs,
        )

    @classmethod
    def generate_candidate_from_trend(
        cls,
        trend_observation: ResearchTrendObservation,
        topic: str | None = None,
    ) -> OpportunityCandidate:
        """Convert a validated ResearchTrendObservation into an OpportunityCandidate hypothesis.

        Enforces:
        - Input must be a valid ResearchTrendObservation.
        - Preserves underlying ResearchEvidenceItem objects (including observation_date and metric_value).
        - Preserves source references without modification.
        - Reuses the trend analysis directly without recomputing or modifying the trend.
        - Keeps FACT (original observations) separate from ANALYSIS (trend direction) and HYPOTHESIS (unvalidated premise).
        - Explicitly documents UNKNOWNS (commercial intent, monetization, customer segments, unit economics).
        - Trend status is not overinterpreted: no conversion into "high demand", "bad opportunity", or "reliable market".
        - Zero scoring, zero ranking, zero automatic opportunity persistence, zero financial effects.
        """
        if not isinstance(trend_observation, ResearchTrendObservation):
            raise ValueError(
                f"Expected ResearchTrendObservation, got {type(trend_observation).__name__}."
            )

        derived_topic = (
            topic.strip()
            if topic and topic.strip()
            else trend_observation.topic
        )

        observation_text = trend_observation.fact_summary

        inference_text = (
            f"Trend analysis indicates that empirical measurements for '{derived_topic}' were "
            f"classified as {trend_observation.status.value} ({trend_observation.analysis}). "
            f"This empirical signal may warrant exploratory opportunity investigation."
        )

        hypothesis_text = (
            f"A viable product, content, or service opportunity addressing needs related to '{derived_topic}' "
            f"may exist, subject to commercial validation."
        )

        unknowns = [
            f"Commercial intent and willingness to pay around '{derived_topic}' are unverified.",
            f"Specific customer segments and problem definitions for '{derived_topic}' are unverified.",
            f"Viable monetization models (product, content, service, affiliate) for '{derived_topic}' are unverified.",
            f"Distribution channels, competition level, and unit economics for '{derived_topic}' are unverified.",
        ]

        return OpportunityCandidate(
            title=f"Opportunity Candidate: {derived_topic}",
            topic=derived_topic,
            observation=observation_text,
            inference=inference_text,
            hypothesis=hypothesis_text,
            unknowns=unknowns,
            evidence_items=list(trend_observation.evidence_items),
            source_references=list(trend_observation.source_references),
            trend_status=trend_observation.status,
        )

    @classmethod
    def generate_from_trend(
        cls,
        trend_observation: ResearchTrendObservation,
        topic: str | None = None,
    ) -> OpportunityCandidate:
        """Alias for generate_candidate_from_trend."""
        return cls.generate_candidate_from_trend(trend_observation, topic=topic)

    @classmethod
    def generate_candidates(
        cls,
        research_input: (
            ResearchCollectionResult
            | Sequence[ResearchEvidenceItem]
            | ResearchTrendObservation
            | Sequence[ResearchTrendObservation]
        ),
    ) -> list[OpportunityCandidate]:
        """Generate candidate hypotheses from collection results, trend observations, or evidence sequences.

        Returns one OpportunityCandidate per distinct evidence observation or trend observation.
        If input is empty, deterministically returns an empty list.
        """
        if isinstance(research_input, str):
            raise ValueError(
                "research_input must be a ResearchCollectionResult, ResearchTrendObservation, or sequence of evidence items, not str."
            )
        elif isinstance(research_input, ResearchCollectionResult):
            evidence_list = research_input.evidence_items
        elif isinstance(research_input, ResearchTrendObservation):
            return [cls.generate_candidate_from_trend(research_input)]
        elif isinstance(research_input, Sequence):
            if not research_input:
                return []
            if all(isinstance(item, ResearchTrendObservation) for item in research_input):
                return [cls.generate_candidate_from_trend(item) for item in research_input]  # type: ignore
            evidence_list = list(research_input)
        else:
            raise ValueError(
                "research_input must be a ResearchCollectionResult, ResearchTrendObservation, or sequence of evidence items."
            )

        if not evidence_list:
            return []

        candidates: list[OpportunityCandidate] = []
        for item in evidence_list:
            if isinstance(item, ResearchTrendObservation):
                candidates.append(cls.generate_candidate_from_trend(item))
            else:
                candidates.append(cls.generate_candidate_from_evidence(item))

        return candidates
