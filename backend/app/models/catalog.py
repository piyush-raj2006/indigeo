from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Dataset(TimestampMixin, Base):
    """Provenance record for every vector/raster layer the platform can analyse."""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(40), index=True)  # boundary|hazard|terrain|infrastructure|exposure
    hazard: Mapped[str | None] = mapped_column(String(40), index=True)
    geometry_type: Mapped[str | None] = mapped_column(String(30))
    srid: Mapped[int] = mapped_column(Integer, default=4326)
    storage: Mapped[str] = mapped_column(String(30), default="spatial_store")  # spatial_store|raster|object_store
    table_name: Mapped[str | None] = mapped_column(String(120))
    raster_path: Mapped[str | None] = mapped_column(String(400))
    feature_count: Mapped[int] = mapped_column(Integer, default=0)
    bbox_min_lon: Mapped[float | None] = mapped_column(Float)
    bbox_min_lat: Mapped[float | None] = mapped_column(Float)
    bbox_max_lon: Mapped[float | None] = mapped_column(Float)
    bbox_max_lat: Mapped[float | None] = mapped_column(Float)
    resolution: Mapped[str | None] = mapped_column(String(120))
    temporal_kind: Mapped[str | None] = mapped_column(String(30))  # static|historical|observed|forecast
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(120), index=True)
    source_url: Mapped[str | None] = mapped_column(String(500))
    license: Mapped[str | None] = mapped_column(String(200))
    attribution: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="ready")  # ready|missing|partial|failed|unverified
    notes: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)  # {"states_covered": n, "missing": [...]}
    origin: Mapped[str] = mapped_column(String(30), default="provider")  # provider|user_upload|synthetic_test

    ingest_runs: Mapped[list["IngestRun"]] = relationship(back_populates="dataset", cascade="all, delete-orphan")


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    rows_skipped: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    dataset: Mapped[Dataset] = relationship(back_populates="ingest_runs")


class AdminUnit(TimestampMixin, Base):
    """Administrative register; geometry is resolved through the spatial store."""

    __tablename__ = "admin_units"
    __table_args__ = (
        UniqueConstraint("kind", "code", name="uq_admin_kind_code"),
        Index("ix_admin_name_kind", "name", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)  # state|ut|district|subdistrict|town|village
    code: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    name_norm: Mapped[str] = mapped_column(String(160), index=True)
    parent_code: Mapped[str | None] = mapped_column(String(40), index=True)
    state_code: Mapped[str | None] = mapped_column(String(12), index=True)
    district_code: Mapped[str | None] = mapped_column(String(12), index=True)
    lon: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    population: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(120), default="")
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class ProviderHealth(Base):
    __tablename__ = "provider_health"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    endpoint: Mapped[str | None] = mapped_column(String(400))
    state: Mapped[str] = mapped_column(String(30), default="unknown")  # healthy|degraded|unavailable|unconfigured
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    detail: Mapped[str | None] = mapped_column(Text)
    requires_auth: Mapped[bool] = mapped_column(default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
