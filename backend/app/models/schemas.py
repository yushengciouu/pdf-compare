from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ImportanceLevel = Literal["low", "medium", "high"]
ChangeType = Literal["added", "removed", "modified"]


CompareMode = Literal["fast", "smart"]
JobStatus = Literal["queued", "running", "done", "failed"]
PageState = Literal["paired", "inserted", "deleted"]


class CompareCreateResponse(BaseModel):
    job_id: str
    status: JobStatus
    mode: CompareMode


class JobProgress(BaseModel):
    current: int = 0
    total: int = 0


class JobStats(BaseModel):
    pages_before: int = 0
    pages_after: int = 0
    paired_pages: int = 0
    inserted_pages: int = 0
    deleted_pages: int = 0
    total_diff_boxes: int = 0


class CompareStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: JobProgress = Field(default_factory=JobProgress)
    mode: CompareMode
    stats: JobStats = Field(default_factory=JobStats)
    created_at: datetime
    expires_at: datetime
    message: str | None = None


class PageMapping(BaseModel):
    before_page: int | None = None
    after_page: int | None = None
    state: PageState


class PageAssets(BaseModel):
    before_image: str | None = None
    after_image: str | None = None
    mask_image: str | None = None
    mask_removed_image: str | None = None
    mask_added_image: str | None = None


class DiffBox(BaseModel):
    x: int
    y: int
    w: int
    h: int
    score: float
    type: str = "content_change"


class ComparePageResponse(BaseModel):
    page_no: int
    mapping: PageMapping
    assets: PageAssets
    boxes: list[DiffBox] = Field(default_factory=list)
    width: int | None = None
    height: int | None = None


# ---------------------------------------------------------------------------
# LLM 分析相關 schemas
# ---------------------------------------------------------------------------

ImportanceLevel = Literal["low", "medium", "high"]
ChangeType = Literal["added", "removed", "modified"]


class PageChange(BaseModel):
    type: ChangeType
    description: str


class PageAnalysisResult(BaseModel):
    slot: int
    state: PageState
    before_page: int | None = None
    after_page: int | None = None
    image_diff: float = 0.0
    text_diff: float = 0.0
    reason: str = ""
    importance: ImportanceLevel = "medium"
    summary: str = ""
    changes: list[PageChange] = Field(default_factory=list)


class AnalyzeSummary(BaseModel):
    pages_before: int
    pages_after: int
    total_slots: int
    candidate_pages: int


class AnalyzeResponse(BaseModel):
    summary: AnalyzeSummary
    thresholds: dict
    overall_summary: str = ""
    pages: list[PageAnalysisResult] = Field(default_factory=list)
