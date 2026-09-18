"""Canonical evidence classification for VentureBot (Section 17)."""

from enum import Enum


class EvidenceCategory(str, Enum):
    """Cognitive categories defined in Section 17 of VENTUREBOT_ARCHITECTURE.md.

    - FACT: Verified, measurable historical data (must have source reference or ledger record).
    - INFERENCE: Logical deduction derived from facts.
    - HYPOTHESIS: Testable premise for an experiment.
    - PREDICTION: Expected outcome or forward estimate (with confidence/uncertainty).
    """

    FACT = "fact"
    INFERENCE = "inference"
    HYPOTHESIS = "hypothesis"
    PREDICTION = "prediction"
