from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

from .catalog import utcnow


class Facility(Base):
    """Shelters, hospitals, schools and other exposed/Response infrastructure.

    Geometry is mirrored into the spatial store so radius/nearest queries stay spatial.
    """

    __tablename__ = "facilities"
    __table_args__ = (Index("ix_facility_type_admin", "facility_type", "district_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    osm_id: Mapped[str | None] = mapped_column(String(40), index=True)
    facility_type: Mapped[str] = mapped_column(String(40), index=True)  # shelter|hospital|school|phc|evacuation_center
    name: Mapped[str | None] = mapped_column(String(250))
    lon: Mapped[float] = mapped_column(Float)
    lat: Mapped[float] = mapped_column(Float)
    state_code: Mapped[str | None] = mapped_column(String(12), index=True)
    district_code: Mapped[str | None] = mapped_column(String(12), index=True)
    admin_names: Mapped[str | None] = mapped_column(String(300))
    capacity_persons: Mapped[int | None] = mapped_column(Integer)
    beds: Mapped[int | None] = mapped_column(Integer)
    verified_capacity: Mapped[bool] = mapped_column(Boolean, default=False)
    accessibility_notes: Mapped[str | None] = mapped_column(Text)
    address: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), default="openstreetmap")
    confidence: Mapped[str] = mapped_column(String(20), default="unverified")  # verified|unverified|derived
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    hazard: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(250))
    description: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), default="moderate")  # low|moderate|high|critical
    lon: Mapped[float] = mapped_column(Float)
    lat: Mapped[float] = mapped_column(Float)
    radius_m: Mapped[float | None] = mapped_column(Float)
    district_code: Mapped[str | None] = mapped_column(String(12), index=True)
    state_code: Mapped[str | None] = mapped_column(String(12), index=True)
    status: Mapped[str] = mapped_column(String(30), default="open", index=True)  # open|verified|acting|closed
    population_affected: Mapped[int | None] = mapped_column(Integer)
    medical_needs: Mapped[int | None] = mapped_column(Integer)
    mobility_needs: Mapped[int | None] = mapped_column(Integer)
    verification_source: Mapped[str | None] = mapped_column(String(200))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    official_alert_ref: Mapped[str | None] = mapped_column(String(300))
    reported_by: Mapped[str] = mapped_column(String(80), default="operator")
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approval_state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    # pending|approved|rejected|not_required - evacuation ordering always needs a human
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_note: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)


class RoadRestriction(Base):
    """Verified closures only. Unverified reports are stored separately from routing inputs."""

    __tablename__ = "road_restrictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    osm_id: Mapped[str | None] = mapped_column(String(40), index=True)
    name: Mapped[str | None] = mapped_column(String(250))
    restriction: Mapped[str] = mapped_column(String(40), default="closed")  # closed|bridge_weight|flooded|unverified
    verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source: Mapped[str] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(String(500))
    reported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lon: Mapped[float | None] = mapped_column(Float)
    lat: Mapped[float | None] = mapped_column(Float)
    district_code: Mapped[str | None] = mapped_column(String(12), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RoutePlan(Base):
    __tablename__ = "route_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    method: Mapped[str] = mapped_column(String(40), default="graph")  # graph|osrm
    origin: Mapped[dict] = mapped_column(JSON, default=dict)
    destination: Mapped[dict] = mapped_column(JSON, default=dict)
    legs: Mapped[dict] = mapped_column(JSON, default=list)
    distance_m: Mapped[float | None] = mapped_column(Float)
    duration_s: Mapped[float | None] = mapped_column(Float)
    feasible: Mapped[bool] = mapped_column(Boolean, default=True)
    excluded_features: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[dict] = mapped_column(JSON, default=list)


class AllocationPlan(Base):
    """OR-Tools relocation/resource allocation result with its constraint report."""

    __tablename__ = "allocation_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    solver: Mapped[str] = mapped_column(String(40), default="ortools_cp_sat")
    objective: Mapped[str | None] = mapped_column(Text)
    assignments: Mapped[dict] = mapped_column(JSON, default=list)
    unassigned: Mapped[dict] = mapped_column(JSON, default=dict)
    constraints: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="optimal")
    requires_human_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    approval_state: Mapped[str] = mapped_column(String(20), default="pending")
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_entity", "entity", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(80), default="anonymous")
    role: Mapped[str] = mapped_column(String(30), default="viewer")
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(40))
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text)
