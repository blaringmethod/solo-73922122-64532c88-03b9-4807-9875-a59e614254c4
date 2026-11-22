"""Pydantic request/response models."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_BLOCKS = 100


class RectIn(BaseModel):
    block_no: str = Field(..., min_length=1, max_length=64, description="块编号，同年唯一")
    x1: int
    y1: int
    x2: int
    y2: int

    @field_validator("block_no")
    @classmethod
    def no_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("块编号不能为空")
        return v


class BoundaryUpload(BaseModel):
    year: int = Field(..., ge=1900, le=2999)
    blocks: list[RectIn] = Field(..., max_length=MAX_BLOCKS)


class CountReport(BaseModel):
    year: int = Field(..., ge=1900, le=2999)
    block_no: str = Field(..., min_length=1, max_length=64)
    households: int = Field(..., ge=0, description="住户数（非负整数）")
    boundary_rev: int = Field(..., ge=1, description="该块现行边界修订号")


class BlockOut(BaseModel):
    year: int
    block_no: str
    x1: int
    y1: int
    x2: int
    y2: int
    boundary_rev: int
    households: int | None


class BlockRef(BaseModel):
    year: int
    block_no: str
    boundary_rev: int
    households: int | None = None
    area: int


class GroupOut(BaseModel):
    status: Literal["matched", "missing_counts", "range_mismatch"]
    year_a_blocks: list[BlockRef]
    year_b_blocks: list[BlockRef]
    households_a_total: int | None
    households_b_total: int | None
    difference_b_minus_a: int | None
    missing: list[BlockRef]
    reason: str | None = None


class ComparisonOut(BaseModel):
    comparison_id: int
    state_id: int
    year_a: int
    year_b: int
    reused_range_result: bool
    all_complete: bool
    groups: list[GroupOut]


class BoundaryUploadResult(BaseModel):
    state_id: int
    year: int
    block_count: int
    retained_blocks: list[str]
    reset_blocks: list[str]
    removed_blocks: list[str]
    blocks: list[BlockOut]


class CountReportResult(BaseModel):
    state_id: int
    block: BlockOut


class StateOut(BaseModel):
    state_id: int
    history: list[dict]
