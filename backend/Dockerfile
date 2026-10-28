# IndiGeo API image.
#
# Build from the repository root:
#   docker build -f backend/Dockerfile -t indigeo-api ./backend
#
# Deliberately single-worker: the spatial store is a local DuckDB file (single writer) and
# analysis jobs run on an in-process thread pool. Raising --workers would open competing
# writers and duplicate job executors. Scale by moving to PostGIS plus an external queue.
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgomp1 is the OpenMP runtime LightGBM/XGBoost import; curl backs the healthcheck.
# GDAL/GEOS/PROJ are NOT installed as system packages: rasterio, pyogrio and shapely
# ship manylinux wheels that bundle them, which is what the lock file resolves.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt

COPY app ./app

# All writable state lives under /data so it can be a named volume.
ENV DATA_DIR=/data \
    UPLOADS_DIR=/data/uploads \
    LAYERS_DIR=/data/layers \
    DEM_DIR=/data/dem \
    PROVIDER_CACHE_DIR=/data/http_cache \
    ARTIFACTS_DIR=/data/artifacts \
    DATABASE_URL=sqlite:////data/indigeo_relational.db \
    SPATIAL_DUCKDB_PATH=/data/indigeo_spatial.duckdb \
    HOST=0.0.0.0 \
    PORT=8000

RUN useradd --create-home --uid 10001 indigeo \
    && mkdir -p /data \
    && chown -R indigeo:indigeo /data /app
# Docker keeps HOME=/root across USER switches, which is not writable by indigeo; DuckDB
# caches its extensions under $HOME/.duckdb.
ENV HOME=/home/indigeo
USER indigeo

# Bake the spatial extension into the image: the store runs INSTALL/LOAD spatial on every
# fresh connection, and without this the first layer read needs egress to
# extensions.duckdb.org. Egress is needed during the build, not at request time.
RUN python -c "import duckdb; con = duckdb.connect(); con.execute('INSTALL spatial'); con.close()"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8000}/api/v1/health" >/dev/null || exit 1

# Render (and most PaaS) inject PORT and route traffic to it; binding the build-time default
# would fail the port scan. Shell form so ${PORT} expands.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
