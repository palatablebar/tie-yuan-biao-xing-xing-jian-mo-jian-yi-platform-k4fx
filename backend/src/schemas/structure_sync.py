from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SyncColumn(BaseModel):
    column: str = Field(min_length=1, max_length=80)
    dtype: Literal["int", "decimal", "date", "string", "bool"]


class SyncChange(BaseModel):
    """显式迁移规则：目前仅用于声明改名（old_name → column），其余由快照 diff 推导。"""

    change_kind: Literal["renamed"]
    old_name: str = Field(min_length=1, max_length=80)
    column: str = Field(min_length=1, max_length=80)


class StructureSyncIn(BaseModel):
    # 新结构：列名 + 类型；与抽样行（sample_rows，可选）一起代表上游最新贴源结构
    columns: list[SyncColumn] = Field(min_length=1, max_length=500)
    sample_rows: list[dict] = Field(default_factory=list, max_length=200)
    row_count: int = Field(default=0, ge=0, le=10_000_000_000)
    changes: list[SyncChange] = Field(default_factory=list, max_length=200)
    note: str | None = Field(default=None, max_length=2000)
