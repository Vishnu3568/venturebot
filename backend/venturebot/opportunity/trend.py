"""Deterministic Research Trend Analysis for VentureBot (Step 22).

Analyzes multiple dated empirical ResearchEvidenceItem observations to evaluate
directional trend without inventing scores, rankings, thresholds, or commercial claims.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from venturebot.opportunity.models import (
    ResearchEvidenceItem,
    ResearchTrendObservation,
    TrendStatus,
)


class ResearchTrendAnalyzer:
    """Pure, deterministic analytical boundary for evaluating trends across dated observations.

    Strictly separates:
    - FACT: empirical observation values and dates preserved from ResearchEvidenceItem
    - ANALYSIS: mathematical directional change over time (INCREASING, DECREASING, STABLE, NO_DIRECTIONAL_CHANGE, INSUFFICIENT_DATA)
    - STATUS: objective classification of trend direction

    Guarantees:
    - Zero scoring, ranking, or confidence ratings.
    - Zero commercial claims (no market demand, customer willingness to pay, or business viability).
    - Zero database writes or financial mutations.
    - Deterministic output regardless of input ordering.
    """

    @classmethod
    def extract_topic_from_statement(cls, statement: str) -> str:
        """Deterministically extract the topic from an evidence statement."""
        clean = statement.strip() if statement else ""
        if not clean:
            return "Unspecified Topic"

        first_quote = clean.find("'")
        if first_quote != -1:
            second_quote = clean.find("'", first_quote + 1)
            if second_quote != -1:
                extracted = clean[first_quote + 1 : second_quote].strip()
                if extracted:
                    return extracted

        return clean

    @classmethod
    def extract_metric_value(cls, item: ResearchEvidenceItem) -> Decimal:
        """Extract or resolve the numeric metric value from a ResearchEvidenceItem."""
        if item.metric_value is not None:
            return Decimal(item.metric_value)

        # Fallback 1: specific pageviews pattern
        pv_match = re.search(r"recorded\s+(\d+(?:\.\d+)?)\s+pageviews", item.statement, re.IGNORECASE)
        if pv_match:
            return Decimal(pv_match.group(1))

        # Fallback 2: first numeric sequence in statement
        num_match = re.search(r"(\d+(?:\.\d+)?)", item.statement)
        if num_match:
            return Decimal(num_match.group(1))

        raise ValueError(
            f"Evidence item '{item.statement}' lacks a numeric metric_value and no numeric value could be extracted."
        )

    @classmethod
    def analyze_trend(
        cls,
        evidence: ResearchEvidenceItem | Sequence[ResearchEvidenceItem],
        topic: str | None = None,
    ) -> ResearchTrendObservation:
        """Analyze one or more dated ResearchEvidenceItem observations to evaluate directional movement.

        Enforces:
        - Input must be a ResearchEvidenceItem or Sequence of ResearchEvidenceItem (strings rejected).
        - At least one evidence item is required.
        - Every evidence item must contain an observation_date (missing dates rejected).
        - Every evidence item must contain or resolve to a numeric metric value.
        - Input items are sorted chronologically by observation_date ascending.
        - Conflicting values on identical dates are rejected.
        - A single observation returns TrendStatus.INSUFFICIENT_DATA.
        - Evaluates INCREASING, DECREASING, STABLE, or NO_DIRECTIONAL_CHANGE across 2+ dated observations.
        - Returns a transient ResearchTrendObservation with zero commercial or speculative claims.
        """
        if isinstance(evidence, str):
            raise ValueError(
                "evidence must be a ResearchEvidenceItem or a sequence of ResearchEvidenceItem, not str."
            )
        elif isinstance(evidence, ResearchEvidenceItem):
            items = [evidence]
        elif isinstance(evidence, Sequence):
            items = list(evidence)
        else:
            raise ValueError("evidence must be a ResearchEvidenceItem or a sequence of ResearchEvidenceItem.")

        if not items:
            raise ValueError("At least one ResearchEvidenceItem is required for trend analysis.")

        # Validate types, dates, and extract values
        extracted: list[tuple] = []
        for it in items:
            if not isinstance(it, ResearchEvidenceItem):
                raise ValueError(f"Expected ResearchEvidenceItem, got {type(it).__name__}.")
            if it.observation_date is None:
                raise ValueError(
                    f"Evidence item missing observation_date: '{it.statement}'. "
                    "All evidence items for trend analysis must have a valid observation_date."
                )
            metric_val = cls.extract_metric_value(it)
            extracted.append((it.observation_date, metric_val, it))

        # Sort chronologically ascending
        sorted_records = sorted(extracted, key=lambda r: r[0])

        # Check for conflicting observations on the same date
        for i in range(len(sorted_records) - 1):
            if sorted_records[i][0] == sorted_records[i + 1][0]:
                if sorted_records[i][1] != sorted_records[i + 1][1]:
                    raise ValueError(
                        f"Conflicting observations recorded on the same date {sorted_records[i][0]}: "
                        f"values {sorted_records[i][1]} vs {sorted_records[i + 1][1]}."
                    )

        # Deduplicate identical records on the same date
        deduped_records: list[tuple] = []
        for rec in sorted_records:
            if not deduped_records or deduped_records[-1][0] != rec[0]:
                deduped_records.append(rec)

        # Derive topic
        if topic and topic.strip():
            derived_topic = topic.strip()
        else:
            derived_topic = cls.extract_topic_from_statement(deduped_records[0][2].statement)

        # Unique source references
        source_refs: list[str] = []
        for _, _, it in deduped_records:
            if it.source_reference and it.source_reference not in source_refs:
                source_refs.append(it.source_reference)

        # Compile factual summary (Layer 1: FACT)
        obs_details = [f"[{d.isoformat()}]: {val}" for d, val, _ in deduped_records]
        fact_summary = (
            f"Empirical observation(s) for topic '{derived_topic}' ({len(deduped_records)} record(s)): "
            + "; ".join(obs_details)
        )

        # Evaluate trend direction (Layer 2: ANALYSIS)
        if len(deduped_records) == 1:
            status = TrendStatus.INSUFFICIENT_DATA
            single_date, single_val, _ = deduped_records[0]
            analysis = (
                f"Single observation recorded on {single_date.isoformat()} (value: {single_val}). "
                "A minimum of two dated observations is required to determine a trend direction."
            )
        else:
            dates = [r[0] for r in deduped_records]
            vals = [r[1] for r in deduped_records]
            diffs = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
            net_change = vals[-1] - vals[0]
            start_date_str = dates[0].isoformat()
            end_date_str = dates[-1].isoformat()

            if all(d == 0 for d in diffs):
                status = TrendStatus.STABLE
                analysis = (
                    f"Supplied observations remained constant at {vals[0]} from {start_date_str} to "
                    f"{end_date_str} across {len(vals)} recorded observations."
                )
            elif all(d >= 0 for d in diffs) and any(d > 0 for d in diffs):
                status = TrendStatus.INCREASING
                analysis = (
                    f"Supplied observations increased from {vals[0]} on {start_date_str} to "
                    f"{vals[-1]} on {end_date_str} across {len(vals)} recorded observations "
                    f"(net change: +{net_change})."
                )
            elif all(d <= 0 for d in diffs) and any(d < 0 for d in diffs):
                status = TrendStatus.DECREASING
                analysis = (
                    f"Supplied observations decreased from {vals[0]} on {start_date_str} to "
                    f"{vals[-1]} on {end_date_str} across {len(vals)} recorded observations "
                    f"(net change: {net_change})."
                )
            else:
                status = TrendStatus.NO_DIRECTIONAL_CHANGE
                analysis = (
                    f"Supplied observations fluctuated between {start_date_str} and {end_date_str} "
                    f"across {len(vals)} recorded observations without a consistent directional change "
                    f"(net change: {net_change:+})."
                )

        return ResearchTrendObservation(
            topic=derived_topic,
            status=status,
            fact_summary=fact_summary,
            analysis=analysis,
            evidence_items=[r[2] for r in deduped_records],
            source_references=source_refs,
        )
