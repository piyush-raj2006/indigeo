"""Lifeline lookups against operator-supplied OpenStreetMap extracts.

The public Overpass API is the weakest link in the evidence path: measured on
2026-10-25, four layers took 504-624 s and still lost layers to 502/504 responses. When a
deployment owns a regional OSM extract, this module answers the same question from that
file instead - no network, milliseconds instead of minutes, and a bbox that either covers
the query or is reported as not covering it.

What counts as an extract is deliberately narrow and auditable:

* the file must be listed in ``data/osm_extracts/manifest.json`` with a bbox and (ideally)
  a source and vintage; a file dropped into the directory but not listed is not used;
* the file must fully contain the query bbox - a partial overlap is a miss, because
  counting only the features that happened to fall inside the file would understate the
  lifelines in the radius;
* anything the file cannot answer (unknown layer, unreadable driver) is reported as
  unavailable per layer, so the caller can fall back to Overpass for exactly that layer.

Supported formats are whatever the installed GDAL drivers can read. ``pyogrio`` ships with
the OSM driver here, so a ``.pbf`` extract works without ``pyrosm``; GeoJSON, GeoPackage,
FlatGeobuf and Shapefile work too. ``pyrosm``/``osmnx`` are not required and are not
assumed to be installed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..core.config import get_settings
from ..core.errors import ProviderUnavailableError
from ..core.logging import get_logger
from ..gis.coords import haversine_m, meters_to_degrees
from .base import Provider, ProviderStatus

log = get_logger("integrations.local_osm")

# Columns a vector layer needs before it can answer an infrastructure layer. A layer that
# has none of them is skipped rather than read and guessed at.
INFRA_COLUMNS: dict[str, tuple[str, ...]] = {
    "bridges": ("highway", "bridge"),
    "major_roads": ("highway",),
    "hospitals": ("amenity",),
    "emergency_services": ("emergency",),
    "schools": ("amenity",),
    "water_structures": ("man_made", "waterway"),
}

# Attribute columns copied into a feature's ``tags`` map. The full row is not exported: a
# GPKG extract can carry dozens of columns and most are not evidence for a lifeline.
TAG_COLUMNS = (
    "amenity",
    "emergency",
    "highway",
    "bridge",
    "waterway",
    "man_made",
    "railway",
    "name",
    "operator",
    "ref",
    "layer",
)

# Which GDAL OSM layers can carry each lifeline. GeoJSON/GeoPackage extracts answer this
# from their columns instead; this table only applies to `.pbf` reads.
_OSM_LAYERS: dict[str, tuple[str, ...]] = {
    "bridges": ("lines",),
    "major_roads": ("lines",),
    "hospitals": ("points", "multipolygons"),
    "emergency_services": ("points", "multipolygons"),
    "schools": ("points", "multipolygons"),
    "water_structures": ("points", "lines", "multipolygons"),
}

_LAYER_CACHE: dict[tuple[str, float, str, float, float, float, float], list[dict[str, Any]]] = {}
_LAYER_CACHE_MAX = 128


@dataclass
class ExtractSpec:
    """One manifest entry: a file this deployment is allowed to read."""

    name: str
    path: Path
    bbox: tuple[float, float, float, float] | None
    vintage: str | None = None
    source: str | None = None
    license: str | None = None
    layer_map: dict[str, str] = field(default_factory=dict)
    notes: str | None = None

    @property
    def resolved(self) -> bool:
        return self.path.exists()

    def bbox_or_read(self) -> tuple[float, float, float, float] | None:
        if self.bbox is not None:
            return self.bbox
        return _file_bounds_4326(self.path)

    def contains(self, box: tuple[float, float, float, float]) -> bool:
        outer = self.bbox_or_read()
        if outer is None:
            return False
        west, south, east, north = outer
        qw, qs, qe, qn = box
        return qw >= west and qs >= south and qe <= east and qn <= north


class LocalOSMProvider(Provider):
    """Read lifelines from a manifest of local OSM extracts.

    ``key`` is ``osm_local`` so provenance and the analysis payload can say which origin
    answered. It never performs HTTP, so ``base_url`` stays empty and ``healthcheck``
    reports manifest state instead of reaching a server.
    """

    key = "osm_local"
    base_url = ""
    license = "ODbL 1.0 (OpenStreetMap contributors)"
    attribution = "Map data (c) OpenStreetMap contributors, ODbL - https://osm.org/copyright"
    requires_auth = False
    cacheable = False

    def __init__(self) -> None:
        super().__init__()
        self._manifest_lock = threading.Lock()
        self._manifest_stamp: tuple[float, int, str] | None = None
        self._extracts: list[ExtractSpec] = []
        self.manifest_path = Path(self._settings.osm_extract_dir) / self._settings.osm_manifest_name
        self.manifest_error: str | None = None
        self._load_manifest(force=True)

    # -- manifest ---------------------------------------------------------
    @property
    def extract_count(self) -> int:
        return sum(1 for spec in self._extracts if spec.resolved)

    def _load_manifest(self, *, force: bool = False) -> None:
        path = Path(self._settings.osm_extract_dir) / self._settings.osm_manifest_name
        self.manifest_path = path
        try:
            stat = path.stat()
            stamp = (stat.st_mtime, stat.st_size, str(path))
        except OSError:
            self._extracts = []
            self._manifest_stamp = None
            self.manifest_error = (
                f"No local OSM extract manifest at {path}. Lifelines fall back to Overpass; "
                "create the manifest to serve them locally."
            )
            return
        if not force and stamp == self._manifest_stamp:
            return
        with self._manifest_lock:
            if not force and stamp == self._manifest_stamp:
                return
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self._extracts = []
                self._manifest_stamp = None
                self.manifest_error = f"Local OSM manifest unreadable ({type(exc).__name__}): {exc}"
                log.warning("OSM extract manifest unreadable", extra={"fields": {"path": str(path), "error": str(exc)}})
                return
            specs: list[ExtractSpec] = []
            entries = raw.get("extracts", []) if isinstance(raw, dict) else raw
            problems: list[str] = []
            for index, entry in enumerate(entries if isinstance(entries, list) else []):
                spec = _spec_from_entry(entry, path.parent, index, problems)
                if spec is not None:
                    specs.append(spec)
            self._extracts = specs
            self._manifest_stamp = stamp
            self.manifest_error = "; ".join(problems) if problems else None
            if problems:
                log.warning(
                    "Some OSM manifest entries were skipped",
                    extra={"fields": {"count": len(problems), "problems": problems[:5]}},
                )

    def extracts(self) -> list[ExtractSpec]:
        """Re-read the manifest when it changed on disk, then return the usable entries."""
        self._load_manifest()
        return [spec for spec in self._extracts if spec.resolved]

    # -- coverage ---------------------------------------------------------
    @staticmethod
    def query_bbox(lon: float, lat: float, radius_m: float) -> tuple[float, float, float, float]:
        dlon, dlat = meters_to_degrees(lat, radius_m)
        return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)

    def covering_extracts(self, lon: float, lat: float, radius_m: float) -> tuple[list[ExtractSpec], tuple[float, float, float, float]]:
        box = self.query_bbox(lon, lat, radius_m)
        covering = [spec for spec in self.extracts() if spec.contains(box)]
        # Newest vintage first. If two overlap, both are read and features de-duplicated,
        # so a newer extract cannot silently hide a feature the older one alone contains.
        covering.sort(key=lambda spec: (spec.vintage or "", spec.name), reverse=True)
        return covering, box

    # -- layer reads ------------------------------------------------------
    def _read_layer(self, spec: ExtractSpec, layer_name: str, box: tuple[float, float, float, float]):
        import pyogrio

        key = (str(spec.path), spec.path.stat().st_mtime, layer_name, *[round(v, 6) for v in box])
        cached = _LAYER_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            frame = pyogrio.read_dataframe(spec.path, layer=layer_name, bbox=box)
        except Exception as exc:
            raise ProviderUnavailableError(
                self.key,
                f"Could not read layer {layer_name!r} from {spec.path.name}: {type(exc).__name__}: {exc}",
            ) from exc
        if frame.crs is not None and frame.crs.to_epsg() not in (4326, None):
            frame = frame.to_crs(4326)
        if len(_LAYER_CACHE) >= _LAYER_CACHE_MAX:
            _LAYER_CACHE.pop(next(iter(_LAYER_CACHE)))
        _LAYER_CACHE[key] = frame
        return frame

    def _candidate_layers(self, spec: ExtractSpec, infra: str) -> list[str]:
        import pyogrio

        if infra in spec.layer_map:
            return [spec.layer_map[infra]]
        try:
            available = [str(row[0]) for row in pyogrio.list_layers(spec.path)]
        except Exception as exc:
            raise ProviderUnavailableError(
                self.key, f"Could not list layers in {spec.path.name}: {type(exc).__name__}: {exc}"
            ) from exc
        # The GDAL OSM driver exposes `points`, `lines`, `multipolygons`, `multilinestrings`
        # and `other_relations`. A lifeline is not confined to one of them: hospitals and
        # schools are often mapped as areas, water towers as nodes and dams as lines, so the
        # caller reads every layer that can carry the tags and merges the result.
        if {"points", "lines"} <= set(available):
            wanted = _OSM_LAYERS.get(infra, ("points",))
            return [name for name in wanted if name in available]
        wanted = set(INFRA_COLUMNS.get(infra, ()))
        candidates: list[str] = []
        for name in available:
            try:
                fields = {str(f).lower() for f in pyogrio.read_info(spec.path, layer=name).get("fields", [])}
            except Exception:
                continue
            if wanted & fields or "tags" in fields or "other_tags" in fields:
                candidates.append(name)
        return candidates

    def layer_features(self, spec: ExtractSpec, infra: str, box, lon: float, lat: float, limit: int) -> list[dict[str, Any]] | None:
        """Read, filter and normalise one infrastructure layer from one extract.

        ``None`` means this extract has no layer carrying the tags for ``infra``, so the
        layer was never observed here. An empty list means a layer was read and genuinely
        contained no matching feature - a real zero, not a gap.
        """
        candidates = self._candidate_layers(spec, infra)
        if not candidates:
            return None
        merged: dict[str, dict[str, Any]] = {}
        for layer_name in candidates:
            frame = self._read_layer(spec, layer_name, box)
            if frame is None or frame.empty:
                continue
            for position, (_, row) in enumerate(frame.iterrows()):
                tags = _tags_of(row)
                if not _matches(infra, tags):
                    continue
                feature = _normalise_local(row, tags, infra, spec, position, lon, lat)
                if feature is None:
                    continue
                key = feature.get("osm_id") or f"{layer_name}:{position}"
                merged.setdefault(key, feature)
        features = sorted(merged.values(), key=lambda f: f.get("distance_m", 1e12))
        return features[:limit]

    # -- public lookup ----------------------------------------------------
    def infrastructure_near(
        self,
        lon: float,
        lat: float,
        radius_m: float,
        *,
        layers: tuple[str, ...] = ("bridges", "hospitals", "emergency_services"),
        limit: int = 500,
    ) -> dict[str, Any]:
        """Lifelines from local extracts, with the same layer contract as Overpass.

        Every layer carries ``origin="local"``. ``status`` is ``ok`` only when a covering
        extract was readable and the layer was actually computed; ``outside_coverage`` and
        ``unavailable`` both mean the caller should fall back, for that layer only.
        """
        from .osm_infra import INFRA_LAYERS

        started = time.monotonic()
        out: dict[str, Any] = {
            "provider": self.key,
            "layers": {},
            "provenance": [],
            "warnings": [],
            "attribution": self.attribution,
            "license": self.license,
            "deadline_seconds": None,
            "local_status": "unconfigured",
        }
        if not self._settings.osm_local_enabled:
            out["local_status"] = "disabled"
            out["warnings"].append("Local OSM extracts are disabled (OSM_LOCAL_ENABLED=false).")
            for name in layers:
                out["layers"][name] = {
                    "features": [],
                    "status": "unavailable",
                    "reason": "local extracts disabled",
                    "origin": "local",
                    "role": (INFRA_LAYERS.get(name) or {}).get("role"),
                }
            out["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            return out

        extracts = self.extracts()
        if not extracts:
            out["local_status"] = "unconfigured"
            out["warnings"].append(self.manifest_error or "No local OSM extracts are configured.")
            for name in layers:
                out["layers"][name] = {
                    "features": [],
                    "status": "unavailable",
                    "reason": "no local extract manifest",
                    "origin": "local",
                    "role": (INFRA_LAYERS.get(name) or {}).get("role"),
                }
            out["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            return out

        covering, box = self.covering_extracts(lon, lat, radius_m)
        out["query_bbox"] = [round(v, 6) for v in box]
        if self.manifest_error:
            out["warnings"].append(self.manifest_error)
        if not covering:
            out["local_status"] = "outside_coverage"
            names = ", ".join(spec.name for spec in extracts[:4])
            reason = (
                f"No local extract fully contains the query bbox {tuple(round(v, 4) for v in box)}; "
                f"available: {names}."
            )
            out["warnings"].append(reason)
            for name in layers:
                out["layers"][name] = {
                    "features": [],
                    "status": "outside_coverage",
                    "reason": reason,
                    "origin": "local",
                    "role": (INFRA_LAYERS.get(name) or {}).get("role"),
                }
            out["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            return out

        out["local_status"] = "used"
        out["extracts"] = [
            {
                "name": spec.name,
                "file": spec.path.name,
                "vintage": spec.vintage,
                "source": spec.source,
                "bbox": [round(v, 6) for v in (spec.bbox_or_read() or ())],
            }
            for spec in covering
        ]
        for name in layers:
            spec_layer = INFRA_LAYERS.get(name)
            if spec_layer is None:
                out["layers"][name] = {"features": [], "status": "unavailable", "reason": "unknown layer", "origin": "local"}
                continue
            merged: dict[str, dict[str, Any]] = {}
            reasons: list[str] = []
            read_any = False
            for spec in covering:
                try:
                    features = self.layer_features(spec, name, box, lon, lat, limit)
                except ProviderUnavailableError as exc:
                    reasons.append(f"{spec.name}: {exc.message}")
                    continue
                except Exception as exc:  # unexpected driver errors are still per-layer gaps
                    reasons.append(f"{spec.name}: {type(exc).__name__}: {exc}")
                    continue
                if features is None:
                    reasons.append(f"{spec.name}: no layer carries the {name} tags")
                    continue
                read_any = True
                for feature in features:
                    key = feature.get("osm_id") or f"{spec.name}:{feature['record_key']}"
                    merged.setdefault(key, feature)
                # An empty result is still an observation from that extract, so its
                # provenance is recorded even when it contributes no features.
                out["provenance"].append(
                    _provenance(
                        endpoint=_file_uri(spec.path),
                        name=spec.name,
                        layer=name,
                        vintage=spec.vintage,
                        source=spec.source,
                        license_=spec.license,
                        count=len(features),
                        elapsed_ms=(time.monotonic() - started) * 1000.0,
                    )
                )
            features = sorted(merged.values(), key=lambda f: f.get("distance_m", 1e12))[:limit]
            status = "ok" if read_any else "unavailable"
            entry: dict[str, Any] = {
                "features": features,
                "count": len(features),
                "truncated": len(merged) > limit,
                "role": spec_layer.get("role"),
                "origin": "local",
                "status": status,
                "extracts": [spec.name for spec in covering],
            }
            if reasons and not read_any:
                entry["reason"] = "; ".join(reasons[:3])
                out["warnings"].append(f"{name}: {entry['reason']}")
            out["layers"][name] = entry
        out["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 1)
        return out

    def healthcheck(self) -> ProviderStatus:
        extracts = self.extracts()
        if not self._settings.osm_local_enabled:
            return ProviderStatus(self.key, "unconfigured", "Local OSM extracts are disabled.", False, None)
        if not extracts:
            return ProviderStatus(
                self.key,
                "unconfigured",
                self.manifest_error or "No local OSM extract manifest is configured.",
                False,
                str(self.manifest_path),
            )
        readable = [spec for spec in extracts if spec.bbox_or_read() is not None]
        if not readable:
            return ProviderStatus(
                self.key,
                "degraded",
                f"{len(extracts)} manifest extract(s) exist but none could be opened. "
                "Install pyogrio/GDAL support for the file format, then retry.",
                False,
                str(self.manifest_path),
            )
        detail = f"{len(readable)} local extract(s) readable: " + ", ".join(
            f"{spec.name}{' (' + spec.vintage + ')' if spec.vintage else ''}" for spec in readable[:4]
        )
        return ProviderStatus(self.key, "healthy", detail, False, _file_uri(readable[0].path))


# -- manifest helpers ---------------------------------------------------------
def _spec_from_entry(entry: Any, root: Path, index: int, problems: list[str]) -> ExtractSpec | None:
    if not isinstance(entry, dict):
        problems.append(f"entry {index} is not an object")
        return None
    path_value = entry.get("path")
    if not isinstance(path_value, str) or not path_value.strip():
        problems.append(f"entry {index} has no path")
        return None
    path = Path(path_value)
    if not path.is_absolute():
        path = root / path
    bbox = entry.get("bbox")
    parsed_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                parsed_bbox = tuple(float(v) for v in bbox)  # type: ignore[assignment]
            except (TypeError, ValueError):
                problems.append(f"entry {index} bbox is not numeric; bounds will be read from the file")
        else:
            problems.append(f"entry {index} bbox must be [west, south, east, north]")
    layer_map = entry.get("layers") if isinstance(entry.get("layers"), dict) else {}
    return ExtractSpec(
        name=str(entry.get("name") or path.stem),
        path=path,
        bbox=parsed_bbox,
        vintage=str(entry["vintage"]) if entry.get("vintage") else None,
        source=str(entry["source"]) if entry.get("source") else None,
        license=str(entry["license"]) if entry.get("license") else None,
        layer_map={str(k): str(v) for k, v in layer_map.items()},
        notes=str(entry["notes"]) if entry.get("notes") else None,
    )


def _file_bounds_4326(path: Path) -> tuple[float, float, float, float] | None:
    """Bounds in EPSG:4326, reprojected from the file's own CRS when needed."""
    try:
        import pyogrio
        from pyproj import CRS, Transformer

        info = pyogrio.read_info(path)
        bounds = info.get("total_bounds")
        crs = info.get("crs")
        if bounds is None:
            return None
        west, south, east, north = (float(v) for v in bounds)
        if not crs:
            return (west, south, east, north)
        src = CRS.from_user_input(crs)
        if src.to_epsg() == 4326:
            return (west, south, east, north)
        transformer = Transformer.from_crs(src, "EPSG:4326", always_xy=True)
        xs, ys = transformer.transform([west, east, west, east], [south, south, north, north])
        return (min(xs), min(ys), max(xs), max(ys))
    except Exception:
        return None


# -- feature normalisation ----------------------------------------------------
def _tags_of(row: Any) -> dict[str, str]:
    tags: dict[str, str] = {}
    for column in TAG_COLUMNS:
        if column in row.index:
            value = row[column]
            if value is not None and not _is_blank(value):
                tags[column] = str(value)
    raw = row.get("tags") if hasattr(row, "get") else None
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            raw = parsed
    if isinstance(raw, dict):
        for key, value in raw.items():
            key_text = str(key)
            if value is None or key_text in tags or _is_blank(value):
                continue
            tags[key_text] = str(value)
    # The GDAL OSM driver only promotes a fixed set of tags to columns, so `bridge`,
    # `amenity` and most others live in `other_tags` as `"key"=>"value"` pairs. A `.pbf`
    # extract read without this would answer every lifeline with zero features.
    other = row.get("other_tags") if hasattr(row, "get") else None
    if isinstance(other, str) and "=>" in other:
        for key, value in _parse_other_tags(other).items():
            tags.setdefault(key, value)
    return tags


_OTHER_TAG_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"\s*=>\s*"((?:[^"\\]|\\.)*)"')


def _parse_other_tags(text: str) -> dict[str, str]:
    """Parse GDAL's HSTORE rendering of untagged columns.

    Keys present as real columns are output first and left untouched; this only adds the
    tags GDAL did not promote, and unescapes ``\\"`` / ``\\\\`` so values round-trip.
    """
    parsed: dict[str, str] = {}
    for key, value in _OTHER_TAG_PAIR.findall(text):
        parsed.setdefault(key.replace('\\"', '"').replace("\\\\", "\\"), value.replace('\\"', '"').replace("\\\\", "\\"))
    return parsed



def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (dict, list, tuple, set)):
        return len(value) == 0
    try:
        import pandas as pd

        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _tag(tags: dict[str, str], key: str) -> str:
    for candidate, value in tags.items():
        if candidate.lower() == key:
            return value.strip().lower()
    return ""


def _matches(infra: str, tags: dict[str, str]) -> bool:
    """The local equivalent of the Overpass selectors in ``INFRA_LAYERS``.

    Keep the two in step: a local extract that applies a narrower rule than the Overpass
    fallback would return fewer lifelines for the same tags depending on which origin
    answered, with no way to tell from the response.
    """
    if infra == "bridges":
        return bool(_tag(tags, "highway")) and _tag(tags, "bridge") not in ("", "no", "false", "0")
    if infra == "major_roads":
        return _tag(tags, "highway") in {"motorway", "trunk", "primary"}
    if infra == "hospitals":
        return _tag(tags, "amenity") == "hospital"
    if infra == "emergency_services":
        return _tag(tags, "emergency") in {"ambulance_station", "ambulance_service", "fire_station", "police"} or _tag(
            tags, "amenity"
        ) in {"fire_station", "police"}
    if infra == "schools":
        return _tag(tags, "amenity") in {"school", "college", "kindergarten"}
    if infra == "water_structures":
        return _tag(tags, "man_made") in {"reservoir_covered", "water_tower", "dam"} or _tag(tags, "waterway") == "dam"
    return False


def _normalise_local(row: Any, tags: dict[str, str], infra: str, spec: ExtractSpec, position: int, lon: float, lat: float) -> dict[str, Any] | None:
    from shapely.geometry import mapping

    geometry = row.get("geometry") if hasattr(row, "get") else None
    if geometry is None:
        return None
    try:
        if geometry.is_empty:
            return None
    except AttributeError:
        return None
    if geometry.geom_type == "Point":
        point = geometry
        kind = "node"
    elif geometry.geom_type in ("LineString", "MultiLineString"):
        point = geometry.interpolate(0.5, normalized=True)
        kind = "way"
    elif geometry.geom_type in ("Polygon", "MultiPolygon"):
        point = geometry.representative_point()
        kind = "way"
    else:
        return None
    distance = haversine_m(lat, lon, float(point.y), float(point.x))
    osm_id = _osm_id(row, geometry.geom_type)
    feature: dict[str, Any] = {
        "osm_id": osm_id,
        "kind": kind,
        "name": tags.get("name"),
        "tags": tags,
        "distance_m": round(distance, 1),
        "extract": spec.name,
        "record_key": f"{infra}:{position}",
    }
    if geometry.geom_type in ("LineString", "MultiLineString"):
        line = _line_geometry(geometry, mapping)
        if line is not None:
            feature["geometry"] = line
    else:
        feature["lat"] = round(float(point.y), 6)
        feature["lon"] = round(float(point.x), 6)
    return feature


def _osm_id(row: Any, geometry_type: str) -> str | None:
    """Use the source identifier verbatim; only OSM columns get a type prefix.

    The prefix has to match the OSM object namespace or the citation points at the wrong
    object: a node id under ``way/`` is a different feature entirely. GDAL's OSM driver
    reports node ids on ``points``, way ids on ``lines``, and on ``multipolygons`` puts the
    relation id in ``osm_id`` while a plain closed way carries only ``osm_way_id``.

    A plain ``id`` in a user-built GeoJSON is not an OSM id, so prefixing it with
    ``way/`` would invent a meaning the file never claimed.
    """

    def value(column: str) -> str | None:
        if column not in row.index:
            return None
        raw = row[column]
        if raw is None or _is_blank(raw):
            return None
        return str(raw)

    for column in ("osm_id", "osm_way_id", "osm_node_id"):
        text = value(column)
        if text is None:
            continue
        if text.startswith(("node/", "way/", "relation/")):
            return text
        if column == "osm_node_id" or geometry_type == "Point":
            return f"node/{text}"
        if geometry_type in ("Polygon", "MultiPolygon"):
            return f"way/{text}" if column == "osm_way_id" else f"relation/{text}"
        return f"way/{text}"
    for column in ("id", "@id"):
        text = value(column)
        if text is not None:
            return text
    return None


def _line_geometry(geometry: Any, mapping: Any) -> dict[str, Any] | None:
    geo = mapping(geometry)
    if geometry.geom_type == "MultiLineString":
        # Keep the longest part so the stored geometry stays a single LineString, which is
        # what the map and the existing Overpass schema expect.
        coords = max(geo["coordinates"], key=len, default=None)
    else:
        coords = geo.get("coordinates")
    if not coords or len(coords) < 2:
        return None
    return {
        "type": "LineString",
        "coordinates": [[round(float(x), 6), round(float(y), 6)] for x, y in coords],
    }


def _provenance(
    *,
    endpoint: str,
    name: str,
    layer: str,
    vintage: str | None,
    source: str | None,
    license_: str | None,
    count: int,
    elapsed_ms: float,
) -> dict[str, Any]:
    return {
        "provider": LocalOSMProvider.key,
        "endpoint": endpoint,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "http_status": None,
        "elapsed_ms": round(elapsed_ms, 1),
        "from_cache": True,  # a local extract is the deployment's own offline copy
        "license": license_ or LocalOSMProvider.license,
        "attribution": LocalOSMProvider.attribution,
        "notes": {
            "origin": "local_extract",
            "extract": name,
            "layer": layer,
            "vintage": vintage,
            "source": source or "not recorded in the extract manifest",
            "features": count,
        },
    }


def _file_uri(path: Path) -> str:
    return path.resolve().as_uri()


__all__ = [
    "ExtractSpec",
    "INFRA_COLUMNS",
    "LocalOSMProvider",
]
