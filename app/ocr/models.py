"""Текущее OCR-задание и неизменяемые результаты его попыток."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base


class OcrJob(Base):
    __tablename__ = "ocr_jobs"
    entry_id: Mapped[int] = mapped_column(ForeignKey("pipeline_entries.id", ondelete="CASCADE"), primary_key=True)
    source_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    current_run_id: Mapped[int | None] = mapped_column(BigInteger)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OcrRun(Base):
    __tablename__ = "ocr_runs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("pipeline_entries.id", ondelete="CASCADE"), index=True)
    source_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    input_sha256: Mapped[str | None] = mapped_column(Text)
    engine_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    inputs: Mapped[list] = mapped_column(JSONB, server_default="[]", nullable=False)
    results: Mapped[list] = mapped_column(JSONB, server_default="[]", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    completed_inputs: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    total_inputs: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    elapsed_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
