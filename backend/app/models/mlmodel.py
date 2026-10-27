from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

from .catalog import utcnow


class ModelRegistry(Base):
    """Every model must state honestly whether it was trained on real labels."""

    __tablename__ = "model_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    task: Mapped[str] = mapped_column(String(60), index=True)  # susceptibility|extent|vulnerability|forecast|ranking|optimisation
    hazard: Mapped[str | None] = mapped_column(String(30), index=True)
    algorithm: Mapped[str] = mapped_column(String(80))
    version: Mapped[str] = mapped_column(String(40), default="0.1.0")
    status: Mapped[str] = mapped_column(String(30), default="untrained", index=True)
    # status: trained|experimental|untrained|blocked
    artifact_path: Mapped[str | None] = mapped_column(String(400))
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    training_dataset: Mapped[str | None] = mapped_column(String(200))
    label_source: Mapped[str | None] = mapped_column(String(200))
    n_train: Mapped[int | None] = mapped_column(Integer)
    n_test: Mapped[int | None] = mapped_column(Integer)
    n_features: Mapped[int | None] = mapped_column(Integer)
    holdout_strategy: Mapped[str | None] = mapped_column(String(60))
    geographic_scope: Mapped[str | None] = mapped_column(String(200))
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    baseline_metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    features: Mapped[dict] = mapped_column(JSON, default=list)
    class_balance: Mapped[dict] = mapped_column(JSON, default=dict)
    shap_available: Mapped[bool] = mapped_column(Boolean, default=False)
    inference_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    limitations: Mapped[str | None] = mapped_column(Text)
    blocker: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    model_key: Mapped[str] = mapped_column(String(80), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="running")
    rows: Mapped[int | None] = mapped_column(Integer)
    positive_rate: Mapped[float | None] = mapped_column(Float)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    log: Mapped[str | None] = mapped_column(Text)
