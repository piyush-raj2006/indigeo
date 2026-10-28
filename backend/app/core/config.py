from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "IndiGeo"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"

    # Relational store. Postgres/PostGIS is the production target; SQLite keeps the
    # prototype runnable without a server.
    database_url: str = Field(default=f"sqlite:///{(PROJECT_ROOT / 'data' / 'indigeo_relational.db').as_posix()}")
    sql_echo: bool = False

    # Spatial store: "duckdb" (local, zero-install) or "postgis" (production).
    spatial_backend: str = "duckdb"
    spatial_duckdb_path: str = Field(default=(PROJECT_ROOT / "data" / "indigeo_spatial.duckdb").as_posix())
    spatial_srid: int = 4326

    data_dir: Path = PROJECT_ROOT / "data"
    uploads_dir: Path = PROJECT_ROOT / "data" / "uploads"
    layers_dir: Path = PROJECT_ROOT / "data" / "layers"
    dem_dir: Path = PROJECT_ROOT / "data" / "dem"
    artifacts_dir: Path = PROJECT_ROOT / "ml" / "artifacts"

    # Local vector extracts. A manifest lists the regional OSM files this deployment
    # actually owns; without one the engine falls back to Overpass and says so.
    osm_extract_dir: Path = PROJECT_ROOT / "data" / "osm_extracts"
    osm_local_enabled: bool = True
    osm_manifest_name: str = "manifest.json"

    # Local raster DEMs (SRTM 30 m, ISRO Bhuvan GeoTIFF, ...) live beside the Terrarium
    # tile cache. The manifest is authoritative: a raster that is not listed is not used.
    dem_local_enabled: bool = True
    dem_manifest_name: str = "manifest.json"
    dem_max_window_pixels: int = 2048

    max_upload_bytes: int = 200 * 1024 * 1024
    max_archive_members: int = 2000
    max_member_bytes: int = 512 * 1024 * 1024
    max_total_uncompressed_bytes: int = 1024 * 1024 * 1024

    http_timeout_seconds: float = 20.0
    http_max_retries: int = 3
    provider_cache_dir: str = Field(default=(PROJECT_ROOT / "data" / "http_cache").as_posix())
    provider_cache_ttl_seconds: int = 6 * 3600

    # In-process caches for reads that are cheap to recompute but expensive to repeat.
    # Set CACHE_ENABLED=false to force every request through to the providers.
    cache_enabled: bool = True
    geocode_cache_ttl_seconds: int = 600
    geocode_cache_max_entries: int = 512
    point_cache_ttl_seconds: int = 300
    point_cache_max_entries: int = 256

    # Background analysis jobs. One worker per slot; SQLite/DuckDB are single-writer files,
    # so raising this multiplies waiting, not throughput, unless the database is moved too.
    analysis_workers: int = 2

    nominatim_user_agent: str = "IndiGeo/0.1 (research prototype; contact set in .env)"
    nominatim_min_interval_seconds: float = 1.1
    # Tried in order. overpass-api.de is the canonical instance but has returned 502
    # "at capacity" throughout this build, so the list keeps working mirrors behind it.
    overpass_mirrors: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "https://overpass-api.de/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
        ]
    )
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    overpass_min_interval_seconds: float = 5.0
    # Wall-clock ceiling for one lifeline lookup across all layers. Serial mirror-walking
    # measured 504-624 s; layers now run concurrently under this budget, and a layer that
    # does not answer inside it is reported unavailable with the elapsed time.
    overpass_deadline_seconds: float = 90.0
    overpass_layer_concurrency: int = 4

    earthdata_token: str | None = None
    # A URL that only answers 200 with Earthdata Login auth. Without it the token's
    # validity is reported as unverified rather than assumed from a public search.
    earthdata_probe_url: str | None = None
    # FIRMS retired the anonymous key: the API answers "Invalid MAP_KEY." for it.
    # A free personal key is required, so the setting stays empty until supplied.
    firms_map_key: str | None = None
    maplibre_style_url: str | None = None

    # Live Indian hazard feeds. No public endpoint is assumed: each connector stays
    # unconfigured until the operator supplies the URL they are licensed to query, and an
    # unconfigured feed is reported as a gap rather than queried at a guessed address.
    imd_alerts_url: str | None = None
    cwc_gauges_url: str | None = None
    cwc_api_key: str | None = None
    bhuvan_hazards_url: str | None = None
    bhuvan_api_key: str | None = None
    live_feed_cache_ttl_seconds: int = 900
    live_feed_timeout_seconds: float = 12.0
    # How far from the analysis point a river gauge still counts as the same basin reach.
    # A 5 km analysis point often sits 10-20 km from the gauge that measures its river.
    cwc_search_radius_m: float = 25_000.0

    # Accepts either JSON (["https://app.example"]) or a comma-separated list. JSON stays
    # supported because earlier deployments (and the docs) used it; CSV is what an operator
    # reaches for first, and rejecting it with a SettingsError was a footgun.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    # Starlette matches allow_origins exactly. Preview deployments rotate their hostname,
    # so they are matched by pattern instead, e.g. ^https://indigeo(-[a-z0-9-]+)?\.vercel\.app$
    cors_origin_regex: str = ""
    api_require_key: bool = False
    api_keys: Annotated[list[str], NoDecode] = Field(default_factory=list)
    emergency_require_approval: bool = True

    @field_validator("cors_origins", "api_keys", "overpass_mirrors", mode="before")
    @classmethod
    def _split_list(cls, value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                return json.loads(text)
            return [part.strip().strip("\"'") for part in text.split(",") if part.strip()]
        return value

    def ensure_dirs(self) -> None:
        for directory in (
            self.data_dir,
            self.uploads_dir,
            self.layers_dir,
            self.dem_dir,
            self.dem_dir / "local",
            self.osm_extract_dir,
            Path(self.provider_cache_dir),
            Path(self.artifacts_dir),
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
