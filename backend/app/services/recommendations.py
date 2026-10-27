from __future__ import annotations

"""Recommendation engine.

Maps scored hazard signals to concrete preparedness / structural / monitoring / policy /
response actions. Every recommendation is evidence-linked: the rationale quotes the factor
values that actually drove the score, and only authorities whose top-level site is used are
cited by homepage (deep document paths are never invented). Actions that require a licensed
professional (structural, hydrological, geotechnical) are explicitly flagged so the platform
never pretends a rule engine is a substitute for engineering assessment.
"""

from typing import Any

from .risk import HazardSignal

BAND_ORDER = {"low": 0, "moderate": 1, "high": 2, "very_high": 3, "extreme": 4, "unassessed": -1}
BAND_LABELS = {"low": "Low", "moderate": "Moderate", "high": "High", "very_high": "Very high", "extreme": "Extreme"}

# Authorities are cited by homepage only; no deep paths are guessed.
AUTHORITIES: dict[str, tuple[str, str | None]] = {
    "ndma": ("National Disaster Management Authority (India)", "https://ndma.gov.in"),
    "mha": ("Ministry of Home Affairs, NDMA Act guidance", "https://mha.gov.in"),
    "imd": ("India Meteorological Department", "https://mausam.imd.gov.in"),
    "incois": ("INCOIS / National Ocean & Environmental Information Service", "https://www.incois.gov.in"),
    "usgs": ("USGS Earthquake Hazards Program", "https://www.usgs.gov"),
    "nrsc": ("ISRO NRSC / Bhuvan geospatial portal", "https://bhuvan.nrsc.gov.in"),
    "cpheeo": ("CPHEEO water & sanitation guidance", "https://cpheeo.gov.in"),
}

# Each rule: hazard -> list of (min_band, category, horizon, action, authority_key, structural)
_RULES: dict[str, list[tuple[str, str, str, str, str, bool]]] = {
    "earthquake": [
        ("high", "preparedness", "immediate", "Secure heavy furniture and water heaters; keep an emergency kit and an out-of-area contact point.", "ndma", False),
        ("high", "response", "immediate", "Drop, cover and hold on during shaking; evacuate damaged structures via pre-planned routes once it stops.", "ndma", False),
        ("moderate", "structural", "long_term", "Have the building assessed for seismic retrofitting against its BIS zone and design basis.", "ndma", True),
        ("moderate", "monitoring", "short_term", "Confirm the official BIS seismic zone for the site; the platform's epicenter climatology is not a zoning substitute.", "usgs", False),
    ],
    "flood": [
        ("high", "response", "immediate", "Move to higher ground and avoid walking or driving through floodwater; follow local flood warnings.", "ndma", False),
        ("moderate", "preparedness", "short_term", "Elevate electrical connections and store documents/essentials above the observed depression level.", "ndma", False),
        ("moderate", "monitoring", "immediate", "Track IMD heavy-rain warnings and river/gauge forecasts for this district.", "imd", False),
        ("high", "structural", "long_term", "Commission drainage and floodplain assessment before construction; a professional hydrological study is required.", "cpheeo", True),
    ],
    "coastal_flood": [
        ("high", "response", "immediate", "If near the shoreline during a surge or king tide, move inland and to elevation immediately.", "incois", False),
        ("moderate", "monitoring", "short_term", "Watch INCOIS storm-surge and high-water advisories for this coast.", "incois", False),
        ("moderate", "structural", "long_term", "Seek coastal-zone-management and setback review before building near the observed low-elevation line.", "mha", True),
    ],
    "cyclone": [
        ("high", "response", "immediate", "Evacuate to a designated sturdy shelter when authorities order it; do not shelter in temporary structures.", "ndma", False),
        ("moderate", "preparedness", "short_term", "Batten windows, secure loose roofing, and charge power banks before landfall.", "imd", False),
        ("moderate", "monitoring", "immediate", "Follow IMD cyclone warnings and the district disaster cell evacuation notices.", "imd", False),
    ],
    "landslide": [
        ("high", "response", "immediate", "If you see ground cracks, new seeps or hear rumbling during rain, move sideways away from the slope, not down.", "ndma", False),
        ("moderate", "structural", "long_term", "Obtain a geotechnical slope-stability review before cutting into or loading the hillside.", "ndma", True),
        ("moderate", "monitoring", "short_term", "Maintain surface drainage and vegetation on cut slopes; watch rainfall thresholds during the monsoon.", "nrsc", False),
    ],
    "heatwave": [
        ("high", "response", "immediate", "Avoid midday sun, hydrate, and check on elderly, outdoor workers and children during peak heat.", "ndma", False),
        ("moderate", "preparedness", "short_term", "Ensure shade, drinking water and a cool room; adjust outdoor work to cooler hours.", "ndma", False),
        ("moderate", "policy", "long_term", "Adopt heat action plan triggers and occupational heat rules for the ward/district.", "mha", False),
    ],
    "drought": [
        ("moderate", "preparedness", "short_term", "Audit water storage and prioritise drinking water; schedule irrigation to soil-moisture shortfalls.", "ndma", False),
        ("moderate", "policy", "long_term", "Coordinate with the district on drought-relief wage works and water-budgeting.", "mha", False),
        ("moderate", "monitoring", "short_term", "Track rainfall and soil-moisture shortfalls; confirm against official IMD/State drought declarations.", "imd", False),
    ],
    "wildfire": [
        ("high", "response", "immediate", "If smoke or fire is observed, evacuate upwind early and report it to the forest/fire services.", "ndma", False),
        ("moderate", "preparedness", "short_term", "Clear flammable vegetation around structures and keep an evacuation route ready.", "ndma", False),
        ("moderate", "monitoring", "short_term", "Enable active-fire monitoring (FIRMS) so detections, not guesses, drive alerts.", "nrsc", False),
    ],
}


def _observed_phrase(signal: HazardSignal) -> str:
    """One honest sentence naming the strongest available drivers and their values."""
    active = [f for f in signal.factors if f.available]
    if not active:
        return "No measured drivers were available for this hazard, so it is reported as unassessed."
    active.sort(key=lambda f: f.severity * f.weight, reverse=True)
    parts = []
    for factor in active[:3]:
        observed = factor.observed if isinstance(factor.observed, dict) else {}
        if observed:
            key, value = next(iter(observed.items()))
            parts.append(f"{factor.name} ({key}={value})")
        else:
            parts.append(factor.name)
    return "Driven by " + "; ".join(parts) + "."


def recommendations_for(signal: HazardSignal) -> list[dict[str, Any]]:
    """Build evidence-linked recommendation rows for one hazard signal."""
    band = signal.as_dict()["band"]  # honours the unassessed case
    if BAND_ORDER.get(band, -1) < 0:
        return []
    rules = _RULES.get(signal.hazard, [])
    rationale = _observed_phrase(signal)
    out: list[dict[str, Any]] = []
    for min_band, category, horizon, action, authority_key, structural in rules:
        if BAND_ORDER[band] < BAND_ORDER[min_band]:
            continue
        authority, url = AUTHORITIES.get(authority_key, (authority_key, None))
        out.append(
            {
                "hazard": signal.hazard,
                "action": action,
                "category": category,
                "horizon": horizon,
                "rationale": f"{BAND_LABELS.get(band, band)} {signal.hazard} risk. {rationale}",
                "evidence": {"signal_score": signal.score, "confidence": signal.confidence, "band": band},
                "authority": authority,
                "guidance_url": url,
                "limitations": (
                    "A licensed professional assessment is required; this rule engine does not replace "
                    "engineering, hydrological or geotechnical design."
                    if structural
                    else "Guidance only. Follow the instructions of local disaster authorities during an actual event."
                ),
                "requires_professional_assessment": structural,
                "score": signal.score,
                "method": "rule_engine",
            }
        )
    return out


def recommend(signals: list[HazardSignal]) -> list[dict[str, Any]]:
    """Recommendations across every signal, strongest hazards first."""
    rows: list[dict[str, Any]] = []
    for signal in sorted(signals, key=lambda s: s.score, reverse=True):
        rows.extend(recommendations_for(signal))
    return rows


__all__ = ["AUTHORITIES", "BAND_LABELS", "BAND_ORDER", "recommend", "recommendations_for"]
