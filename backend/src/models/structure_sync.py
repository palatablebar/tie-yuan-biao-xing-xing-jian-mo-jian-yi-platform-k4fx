"""结构同步记录：每次贴源表结构同步都留痕，可追溯变化前后字段与受影响范围。

失败的同步只落一条 failed 记录，旧结构与旧建议完全保留；成功的同步记录字段 diff、
受影响关系与建议，并把受影响建议标记为 stale（导出资格立即失效）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class StructureSync(Base):
    __tablename__ = "structure_syncs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    table_id: Mapped[int] = mapped_column(ForeignKey("source_tables.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="success", index=True)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    # 变化前后结构快照：[{"column": ..., "dtype": ...}, ...]
    before_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    after_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    # 字段级迁移规则：[{"column", "old_name", "old_dtype", "dtype", "change_kind"}, ...]
    changes: Mapped[list] = mapped_column(JSON, default=list)
    # 受影响范围（隔离到真正引用变化字段的对象）
    affected_relation_ids: Mapped[list] = mapped_column(JSON, default=list)
    affected_suggestion_ids: Mapped[list] = mapped_column(JSON, default=list)
    rebuilt_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("model_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
