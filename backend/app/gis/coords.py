from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..core.errors import InvalidCoordinates

INDIA_BBOX = (68.1, 6.6, 97.4, 37.1)  # west, south, east, north (rough national envelope)
MIN_RADIUS_M = 100.0
MAX_RADIUS_M = 100_000.0

_LAT_LON_RE = re.compile(
    r"^\s*(?P<lat>[-+]?\d{1,2}(?:\.\d+)?)\s*(?:,|;|\s+)\s*(?P<lon>[-+]?\d{1,3}(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)
_DMS_RE = re.compile(
    r"^(?P<deg>\d{1,3})\s*(?:[\u00b0d]|deg)?\s*(?:(?P<min>\d{1,2})\s*(?:['\u2032m]|min)?\s*"
    r"(?:(?P<sec>\d{1,2}(?:\.\d+)?)\s*(?:[\"\u2033s]|sec)?)?)?\s*(?P<hem>[NSEWnsew])?$"
)


@dataclass(frozen=True)
class LatLng:
    lat: float
    lon: float

    def __post_init__(self) -> None:
        validate_latlon(self.lat, self.lon)

    def as_tuple(self) -> tuple[float, float]:
        return (self.lon, self.lat)

    def within_india(self) -> bool:
        return INDIA_BBOX[0] <= self.lon <= INDIA_BBOX[2] and INDIA_BBOX[1] <= self.lat <= INDIA_BBOX[3]

    def __str__(self) -> str:
        return f"{self.lat:.6f}, {self.lon:.6f}"


def validate_latlon(lat: float, lon: float) -> None:
    for name, value, limit in (("latitude", lat, 90.0), ("longitude", lon, 180.0)):
        if value != value or value in (float("inf"), float("-inf")):
            raise InvalidCoordinates(f"{name} must be a finite number.", details={name: value})
        if abs(value) > limit:
            raise InvalidCoordinates(
                f"{name} is outside the valid range.",
                details={name: value, "min": -limit, "max": limit},
            )


def _dms_to_decimal(text: str) -> float | None:
    match = _DMS_RE.match(text.strip())
    if not match:
        return None
    degrees = float(match.group("deg"))
    minutes = float(match.group("min") or 0)
    seconds = float(match.group("sec") or 0)
    value = degrees + minutes / 60.0 + seconds / 3600.0
    hemisphere = (match.group("hem") or "").upper()
    if hemisphere in {"S", "W"}:
        value = -value
    elif hemisphere == "":
        return None
    return value


def parse_coordinates(raw: str) -> LatLng:
    """Parse "20.5937, 78.9629", "20.5937 78.9629", "20.5937N 78.9629E" or DMS pairs."""
    if raw is None:
        raise InvalidCoordinates("No coordinates supplied.")
    text = raw.strip().rstrip(".")
    if not text:
        raise InvalidCoordinates("No coordinates supplied.")

    tokens = [t for t in re.split(r"[,;]| {2,}|\t|\|", text) if t.strip()]
    if len(tokens) == 1:
        tokens = text.split()
    if len(tokens) == 2 and re.search(r"[NSEWnsew]", tokens[0]) and re.search(r"[NSEWnsew]", tokens[1]):
        pass
    elif len(tokens) > 2:
        # "18 31 20 N 75 45 12 E" style
        halves = re.split(r"(?<=[NSEWnsew])\s+(?=[\d])", text)
        if len(halves) == 2:
            tokens = halves

    if len(tokens) != 2:
        raise InvalidCoordinates(
            "Could not read two coordinates. Use \"latitude, longitude\" such as 20.5937, 78.9629.",
            details={"input": raw, "tokens": tokens},
        )

    values: list[float] = []
    for token in tokens:
        token = token.strip()
        signed = re.match(r"^([-+]?[\d.]+)\s*([NSEWnsew])?$", token)
        if signed:
            number = float(signed.group(1))
            hemi = (signed.group(2) or "").upper()
            if hemi in {"S", "W"}:
                number = -number
            values.append(number)
            continue
        dms = _dms_to_decimal(token)
        if dms is None:
            raise InvalidCoordinates(
                f"\"{token}\" is not a recognisable coordinate value.",
                details={"input": raw},
            )
        values.append(dms)

    if all(abs(v) > 90 for v in values):
        raise InvalidCoordinates(
            "Both values exceed the latitude range of +/-90, so they cannot be a lat/lon pair.",
            details={"input": raw},
        )

    lat, lon = values
    try:
        return LatLng(lat=lat, lon=lon)
    except InvalidCoordinates as original:
        # Accept the common "longitude, latitude" ordering only when it is unambiguous.
        try:
            LatLng(lat=lon, lon=lat)
        except InvalidCoordinates:
            raise original
        raise InvalidCoordinates(
            "Values were only valid with the longitude first. IndiGeo expects \"latitude, longitude\".",
            details={"input": raw, "suggested": f"{lon:.6f}, {lat:.6f}"},
        )


_RADIUS_UNITS = {
    "m": 1.0,
    "metre": 1.0,
    "metres": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "km": 1000.0,
    "kms": 1000.0,
    "k": 1000.0,
    "mi": 1609.344,
    "mile": 1609.344,
    "miles": 1609.344,
}
_RADIUS_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)$")


def parse_radius_m(raw: str | float | None, default: float = 5000.0) -> float:
    """Accept 5000, "5000", "3 km", "1.5mi" - a plain number is metres."""
    if raw is None:
        return default
    if isinstance(raw, (int, float)):
        value = float(raw)
    else:
        match = _RADIUS_RE.match(str(raw).strip())
        if not match:
            raise InvalidCoordinates(
                'Radius must look like "2500", "3 km" or "2mi".',
                details={"input": raw},
            )
        unit = match.group(2).lower()
        if unit and unit not in _RADIUS_UNITS:
            raise InvalidCoordinates(
                "Unknown radius unit.",
                details={"input": raw, "supported": sorted(_RADIUS_UNITS)},
            )
        value = float(match.group(1)) * _RADIUS_UNITS.get(unit, 1.0)
    if not MIN_RADIUS_M <= value <= MAX_RADIUS_M:
        raise InvalidCoordinates(
            "Radius must be between 100 m and 100 km.",
            details={"radius_m": value, "input": raw, "min": MIN_RADIUS_M, "max": MAX_RADIUS_M},
        )
    return value


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def meters_to_degrees(lat: float, metres: float) -> tuple[float, float]:
    """Bounding-box half-extents (dlon, dlat) that cover ``metres`` around ``lat``."""
    dlat = metres / 111_320.0
    dlon = metres / max(1.0, 111_320.0 * math.cos(math.radians(lat)))
    return dlon, dlat


def is_indian_bbox(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> bool:
    return not (max_lat < INDIA_BBOX[1] or min_lat > INDIA_BBOX[3] or max_lon < INDIA_BBOX[0] or min_lon > INDIA_BBOX[2])
