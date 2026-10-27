from __future__ import annotations

"""Geometry helpers shared by the spatial fixtures and the boundary tests."""

import shapely


def square_wkb(min_lon: float, min_lat: float, side: float = 1.0) -> bytes:
    """A WKB box, used as a stand-in administrative polygon in offline tests."""
    return shapely.to_wkb(shapely.box(min_lon, min_lat, min_lon + side, min_lat + side))
