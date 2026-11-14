"""导出：DDL（9 段 SQL）+ 关系清单 CSV（UTF-8 BOM）。

硬性口径：DDL 导出必须拒绝任何依据过期（freshness=stale）的建议——
贴源字段一旦变化，含旧列的 DDL 立即失去导出资格，直到管理员确认按新结构重建并重新采纳。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import RELATION_KIND_LABELS, SUGGESTION_FRESHNESS_LABELS, SUGGESTION_ROLE_LABELS, SUGGESTION_STATUS_LABELS
from src.models.model_run import ModelRun
from src.models.model_suggestion import ModelSuggestion
from src.models.relation import Relation
from src.models.source_table import SourceTable
from src.services import star_service
from src.utils.csv_export import to_csv
from src.utils.errors import AppError


def latest_run(db: Session) -> ModelRun | None:
    return db.scalar(select(ModelRun).order_by(ModelRun.id.desc()).limit(1))


def ddl_sql(db: Session) -> dict:
    run = latest_run(db)
    if run is None:
        raise AppError("NOT_FOUND", 404, {"entity": "model_run"})
    rows = db.scalars(
        select(ModelSuggestion).where(ModelSuggestion.run_id == run.id)
    ).all()
    if not rows:
        raise AppError("NOT_FOUND", 404, {"entity": "model_suggestion"})
    stale = [row for row in rows if row.freshness == "stale"]
    if stale:
        # 导出必须拒绝依据过期的建议：宁可拒绝整次导出，也不让旧列 DDL 流出
        raise AppError(
            "EXPORT_STALE",
            409,
            {
                "reason": "存在依据过期的建模建议，DDL 导出已拒绝；请在「结构同步」确认按新结构重建并重新采纳",
                "stale_targets": [row.target_name for row in stale],
                "stale_suggestion_ids": [row.id for row in stale],
                "structure_sync_ids": sorted({row.structure_sync_id for row in stale if row.structure_sync_id}),
            },
        )
    payload = [
        {
            "target_name": row.target_name,
            "role": row.role,
            "confidence": row.confidence,
            "status": SUGGESTION_STATUS_LABELS.get(row.status, row.status),
            "freshness": SUGGESTION_FRESHNESS_LABELS.get(row.freshness, row.freshness),
            "ddl": row.ddl,
        }
        for row in rows
    ]
    sql = star_service.build_export_sql(payload, run.run_no)
    segments = sql.count("-- =====")
    return {"filename": "starmodel-ddl.sql", "sql": sql, "segments": segments, "run_no": run.run_no}


def relations_csv(db: Session) -> bytes:
    relations = db.scalars(select(Relation).order_by(Relation.left_table_id, Relation.left_column)).all()
    tables = {table.id: table for table in db.scalars(select(SourceTable)).all()}
    headers = ["左表", "左字段", "右表", "右字段", "包含度", "置信度", "类型", "抽样行数", "依据状态"]
    rows = []
    for row in relations:
        left = tables.get(row.left_table_id)
        right = tables.get(row.right_table_id)
        rows.append(
            [
                left.name if left else row.left_table_id,
                row.left_column,
                right.name if right else row.right_table_id,
                row.right_column,
                f"{row.containment:.4f}",
                f"{row.confidence:.3f}",
                RELATION_KIND_LABELS.get(row.kind, row.kind),
                row.sample_size,
                SUGGESTION_FRESHNESS_LABELS.get(row.freshness, row.freshness),
            ]
        )
    return to_csv(headers, rows)


def suggestions_csv(db: Session) -> bytes:
    rows = db.scalars(select(ModelSuggestion).order_by(ModelSuggestion.role, ModelSuggestion.target_name)).all()
    tables = {table.id: table for table in db.scalars(select(SourceTable)).all()}
    headers = ["目标表", "源表", "角色", "粒度", "置信度", "状态", "依据状态", "可导出", "度量列", "采纳人"]
    payload = [
        [
            row.target_name,
            tables[row.table_id].name if row.table_id in tables else row.table_id,
            SUGGESTION_ROLE_LABELS.get(row.role, row.role),
            row.grain,
            f"{row.confidence:.3f}",
            SUGGESTION_STATUS_LABELS.get(row.status, row.status),
            SUGGESTION_FRESHNESS_LABELS.get(row.freshness, row.freshness),
            "是" if row.freshness == "current" else "否",
            ",".join(row.measures or []),
            row.adopted_by or "",
        ]
        for row in rows
    ]
    return to_csv(headers, payload)
