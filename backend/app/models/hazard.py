from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

from .catalog import utcnow


class HazardEvent(Base):
    """A recorded or forecast hazard observation with explicit evidence type."""

    __tablename__ = "hazard_events"
    __table_args__ = (
        Index("ix_event_hazard_time", "hazard", "occurred_at"),
        Index("ix_event_admin", "state_code", "district_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hazard: Mapped[str] = mapped_column(String(30), index=True)
    evidence_kind: Mapped[str] = mapped_column(String(20), index=True)  # historical|observed|forecast|model
    source: Mapped[str] = mapped_column(String(120), index=True)
    source_event_id: Mapped[str | None] = mapped_column(String(160), index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lon: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    magnitude: Mapped[float | None] = mapped_column(Float)
    magnitude_scale: Mapped[str | None] = mapped_column(String(20))
    severity_label: Mapped[str | None] = mapped_column(String(40))
    deaths: Mapped[int | None] = mapped_column(Integer)
    affected: Mapped[int | None] = mapped_column(Integer)
    state_code: Mapped[str | None] = mapped_column(String(12))
    district_code: Mapped[str | None] = mapped_column(String(12))
    admin_names: Mapped[str | None] = mapped_column(String(300))
    dataset_code: Mapped[str | None] = mapped_column(String(80), ForeignKey("datasets.code"), index=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_flags: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HazardAnalysis(Base):
    __tablename__ = "hazard_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|running|completed|failed
    hazards: Mapped[dict] = mapped_column(JSON, default=list)
    geometry_kind: Mapped[str] = mapped_column(String(20))  # point|radius|polygon
    lon: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    radius_m: Mapped[float | None] = mapped_column(Float)
    wkt: Mapped[str | None] = mapped_column(Text)
    srid: Mapped[int] = mapped_column(Integer, default=4326)
    admin_context: Mapped[dict] = mapped_column(JSON, default=dict)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    results: Mapped[dict] = mapped_column(JSON, default=dict)
    data_completeness: Mapped[dict] = mapped_column(JSON, default=dict)
    model_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[dict] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    requested_by: Mapped[str | None] = mapped_column(String(80))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (Index("ix_rec_analysis", "analysis_id", "hazard"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analysis_id: Mapped[int | None] = mapped_column(ForeignKey("hazard_analyses.id", ondelete="CASCADE"), index=True)
    analysis_uuid: Mapped[str | None] = mapped_column(String(36), index=True)
    hazard: Mapped[str] = mapped_column(String(30), index=True)
    action: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(40))  # preparedness|structural|monitoring|policy|response
    horizon: Mapped[str] = mapped_column(String(20))  # immediate|short_term|long_term
    rationale: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    authority: Mapped[str | None] = mapped_column(String(200))
    guidance_url: Mapped[str | None] = mapped_column(String(500))
    limitations: Mapped[str | None] = mapped_column(Text)
    requires_professional_assessment: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(40), default="rule_engine")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
