from __future__ import annotations

"""Application entrypoint.

    cd backend && python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.v1.router import health as health_probe, router as v1_router
from .core.config import get_settings
from .core.errors import install_exception_handlers
from .core.logging import configure_logging, get_logger

settings = get_settings()
configure_logging()
log = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the relational schema if missing; never ingest from a web worker."""
    from .core.database import init_schema
    from .services.jobs import reconcile_interrupted_jobs

    try:
        init_schema()
    except Exception as exc:  # the API can still serve provider-free diagnostics
        log.warning("database schema not ready: %s", exc)
    else:
        # A worker thread cannot outlive its process, so jobs left queued/running by a
        # previous run will never finish; marking them keeps pollers from waiting forever.
        try:
            interrupted = reconcile_interrupted_jobs()
            if interrupted:
                log.warning("%s job(s) from a previous process marked interrupted", interrupted)
        except Exception as exc:
            log.warning("could not reconcile interrupted jobs: %s", exc)
    if not settings.cors_origins:
        log.warning("cors_origins is empty: browser clients will be blocked by CORS")
    elif settings.environment not in {"local", "development", "test"} and all(
        origin.startswith(("http://localhost", "http://127.0.0.1")) for origin in settings.cors_origins
    ):
        log.warning(
            "environment=%s but cors_origins only lists local origins (%s); set CORS_ORIGINS "
            "to the deployed frontend origin",
            settings.environment,
            ", ".join(settings.cors_origins),
        )
    log.info(
        "%s %s listening under %s (spatial backend: %s, cors: %s)",
        settings.app_name,
        settings.app_version,
        settings.api_prefix,
        settings.spatial_backend,
        ", ".join(settings.cors_origins) or "none",
    )
    yield


app = FastAPI(
    title=f"{settings.app_name} API",
    version=settings.app_version,
    summary="IndiGeo API — India-wide multi-hazard GIS analysis with provenance on every number.",
    description=(
        "Hazard scores are produced by a rule engine over live public sources (Open-Meteo, "
        "USGS ComCat, Terrarium DEM, the national coastline outline, optional NASA FIRMS). "
        "Every response carries the per-source availability flags, the observations behind "
        "each factor, the provenance of each observation and the limitations of the method. "
        "A source that did not answer reduces confidence; it never fills in a value."
    ),
    docs_url="/docs",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Deployed origins come from configuration, never from source. A literal
    # "https://*.vercel.app" entry does not work: Starlette matches exact strings, so
    # preview deployments belong in CORS_ORIGIN_REGEX.
    allow_origins=settings.cors_origins,
    allow_origin_regex=settings.cors_origin_regex or None,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
install_exception_handlers(app)
# Load balancers and container probes normally ask for /health, but the API lives under a
# prefix; this alias reuses the same handler so both URLs report the same instance state.
app.add_api_route("/health", health_probe, methods=["GET"], include_in_schema=False, tags=["system"])
app.include_router(v1_router, prefix=settings.api_prefix)