from __future__ import annotations

"""Hazard scoring primitives.

This module holds no network, spatial or DEM calls. Everything here is pure so the
weighting, banding and honesty-about-missing-data rules can be unit tested offline.
An assessors module feeds measured evidence into these helpers to build a signal.

The governing rule is: a factor whose underlying data is unavailable is never imputed.
It is dropped from the score and it lowers confidence instead, so a hazard computed from
one surviving driver is reported as low-confidence rather than as if fully observed.
"""

from dataclasses import dataclass, field
from typing import Any

BAND_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (25.0, "low"),
    (50.0, "moderate"),
    (70.0, "high"),
    (85.0, "very_high"),
)


def band_for(score: float) -> str:
    """Map a 0-100 risk score to an ordinal band label."""
    for upper, label in BAND_THRESHOLDS:
        if score < upper:
            return label
    return "extreme"


def saturate(value: float | None, lo: float, hi: float) -> float:
    """Linearly map ``value`` from [lo, hi] onto [0, 1]; out of range is clipped."""
    if value is None or hi == lo:
        return 0.0
    return max(0.0, min(1.0, (float(value) - lo) / (hi - lo)))


def rank_severity(rank: int, of: int) -> float:
    """Severity of an ordinal risk rank (1 = worst) out of ``of`` classes, on 0-1."""
    if of <= 1:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (rank - 1) / (of - 1)))


# Wording used by CAP, IMD bulletins and Bhuvan/NDMA susceptibility maps. ``None`` means
# the label was recognised as "unknown" and must not become a number.
_SEVERITY_WORDS: dict[str, float | None] = {
    "extreme": 1.0,
    "very high": 0.9,
    "severe": 0.75,
    "high": 0.75,
    "moderate": 0.5,
    "medium": 0.5,
    "minor": 0.2,
    "low": 0.2,
    "very low": 0.1,
    "unknown": None,
    "no data": None,
}


def ordinal_severity(value: Any) -> float | None:
    """Map an alert/susceptibility label or class number onto 0-1.

    Strings use the published severity vocabulary. Numbers 0-1 are taken as already
    normalised; numbers 1-5 are treated as an ordinal class where 5 is worst. Anything
    unrecognised returns ``None`` so the caller can report the value instead of guessing.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if 0.0 <= number <= 1.0:
            return max(0.0, min(1.0, number))
        if 1.0 < number <= 5.0:
            return max(0.0, min(1.0, (number - 1.0) / 4.0))
        return None
    text = str(value).strip().lower().replace("_", " ").replace("-", " ")
    if text in _SEVERITY_WORDS:
        return _SEVERITY_WORDS[text]
    for word, severity in _SEVERITY_WORDS.items():
        if word in text:
            return severity
    return None


@dataclass
class Factor:
    """One measured driver behind a hazard score.

    ``available`` is False whenever the source could not answer; in that case
    ``severity`` is ignored and the factor only serves to reduce confidence.
    """

    name: str
    source: str
    severity: float = 0.0  # 0..1 normalised contribution
    weight: float = 1.0
    available: bool = True
    observed: Any = None  # the raw value for transparency
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "source": self.source,
            "weight": round(self.weight, 3),
            "available": self.available,
        }
        if self.available:
            out["severity"] = round(self.severity, 4)
        if self.observed is not None:
            out["observed"] = self.observed
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class HazardSignal:
    """Scored, evidence-backed assessment for a single hazard at one location."""

    hazard: str
    factors: list[Factor] = field(default_factory=list)
    evidence_kind: str = "model"  # historical|observed|forecast|model
    method: str = "rule_engine"
    unavailable_sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    _score: float | None = None

    def _weighted(self) -> tuple[float, float, float]:
        total = sum(f.weight for f in self.factors)
        if total <= 0:
            return 0.0, 0.0, 0.0
        active = [f for f in self.factors if f.available]
        active_weight = sum(f.weight for f in active)
        if active_weight <= 0:
            return 0.0, 0.0, 0.0
        numerator = sum(f.weight * f.severity for f in active)
        # Renormalise over available drivers so a missing source does not silently
        # depress the score; confidence already records how much was actually seen.
        score = 100.0 * (numerator / active_weight)
        confidence = active_weight / total
        return score, confidence, active_weight

    @property
    def score(self) -> float:
        if self._score is not None:
            return self._score
        return round(self._weighted()[0], 1)

    @property
    def confidence(self) -> float:
        return round(self._weighted()[1], 3)

    @property
    def has_evidence(self) -> bool:
        return any(f.available for f in self.factors)

    def as_dict(self) -> dict[str, Any]:
        active = [f for f in self.factors if f.available]
        # With no available driver there is nothing observed, so a 0 score must not be
        # read as "low risk". Report it as unassessed instead.
        band = band_for(self.score) if self.has_evidence else "unassessed"
        return {
            "hazard": self.hazard,
            "score": self.score,
            "band": band,
            "confidence": self.confidence,
            "evidence_kind": self.evidence_kind,
            "method": self.method,
            "data_completeness": {
                "factors_total": len(self.factors),
                "factors_available": len(active),
                "unavailable_sources": list(self.unavailable_sources),
                "confidence": self.confidence,
            },
            "factors": [f.as_dict() for f in self.factors],
            "warnings": list(self.warnings),
            "provenance": list(self.provenance),
            "details": self.details,
        }


def overall_band(signals: list[HazardSignal]) -> str:
    """The headline band is driven by the highest-confidence, highest-score signal."""
    scored = [(s.score, s.confidence) for s in signals if s.has_evidence]
    if not scored:
        return "unknown"
    peak = max(scored)
    return band_for(peak[0])


__all__ = [
    "BAND_THRESHOLDS",
    "Factor",
    "HazardSignal",
    "band_for",
    "ordinal_severity",
    "overall_band",
    "rank_severity",
    "saturate",
]
