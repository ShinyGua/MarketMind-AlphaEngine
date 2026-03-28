"""Pydantic models for MCP tool inputs and outputs."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── market-data-mcp schemas ──────────────────────────────────────────

class PriceHistoryInput(BaseModel):
    tickers: list[str] = Field(description="Ticker symbols, e.g. ['NVDA','SPY']")
    period: str = Field(description="Lookback period: 1d|5d|1mo|3mo|6mo|1y|2y")
    interval: str = Field(description="Bar interval: 1m|5m|15m|1h|1d|1wk|1mo")


class NewsInput(BaseModel):
    query: str = Field(description="Search query")
    endpoint: str = Field(default="everything", description="everything|top-headlines")
    category: Optional[str] = Field(default=None, description="business|technology|general")
    language: str = Field(default="en")
    max_results: int = Field(default=15, ge=1, le=100)
    lookback_hours: int = Field(default=36, ge=1, le=168)
    sort_by: str = Field(default="publishedAt")


class FilingsInput(BaseModel):
    ticker: str
    company_name: Optional[str] = None
    filing_types: list[str] = Field(default=["10-K", "10-Q", "8-K", "4"])
    limit: int = Field(default=5, ge=1, le=40)
    lookback_days: int = Field(default=30, ge=1, le=365)


class MacroSeriesInput(BaseModel):
    series_ids: list[str] = Field(description="FRED series IDs, e.g. ['DGS10','VIXCLS']")
    lookback_months: int = Field(default=3, ge=1, le=24)


class CompanyInfoInput(BaseModel):
    ticker: str


class EarningsCalendarInput(BaseModel):
    ticker: str


# ── workspace-mcp schemas ────────────────────────────────────────────

class WriteArtifactInput(BaseModel):
    ticker: str
    path: str = Field(description="Relative path within workspace")
    content: str
    content_type: str = Field(default="json", description="json|markdown|csv|text")


class UpdateStatusInput(BaseModel):
    ticker: str
    stage: Optional[str] = None
    completed_stage: Optional[str] = None
    run_date: Optional[str] = None
    error: Optional[str] = None


class CreateWorkspaceInput(BaseModel):
    ticker: str


class CreateDateDirsInput(BaseModel):
    ticker: str
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


# ── memory-mcp schemas ───────────────────────────────────────────────

class MemoryTags(BaseModel):
    ticker: Optional[str] = None
    sector: Optional[str] = None
    event_type: Optional[str] = None
    decision: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    horizon: Optional[str] = None
    key_themes: list[str] = Field(default_factory=list)
    importance: Optional[float] = Field(default=None, ge=0, le=1)


class StoreMemoryInput(BaseModel):
    type: str = Field(description="episodic|semantic|procedural")
    content: str
    tags: MemoryTags = Field(default_factory=MemoryTags)
    related_entities: list[str] = Field(default_factory=list)
    source_run: Optional[dict] = None


class SearchMemoryInput(BaseModel):
    query: str
    filters: Optional[dict] = None
    limit: int = Field(default=10, ge=1, le=50)


class EntityTimelineInput(BaseModel):
    entity: str
    limit: int = Field(default=20, ge=1)


class UpdateMemoryInput(BaseModel):
    memory_id: str
    tags: Optional[MemoryTags] = None
    superseded_by: Optional[str] = None


class PruneMemoriesInput(BaseModel):
    older_than_days: int = Field(default=180, ge=1)
    min_importance: float = Field(default=0.3, ge=0, le=1)
    memory_type: Optional[str] = None
