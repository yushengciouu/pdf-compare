from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


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
