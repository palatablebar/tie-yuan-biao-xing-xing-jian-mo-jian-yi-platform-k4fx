"""结构同步服务：可追溯的结构变更、字段级影响隔离、按新结构重建。

事务口径：
- 同步在 SAVEPOINT 内改动结构；任何失败都回滚到同步前（旧版结构 / 关系 / 建议完整保留），
  仅落一条 status=failed 的 structure_syncs 记录与审计，由控制器提交。
- 成功同步：替换抽样行与字段剖析、结构版本 +1，受影响关系 / 建议立即置 stale（导出资格失效），
  无关建议保持 current 且采纳状态不变。
- 管理员确认后 rebuild：只对受影响表重新建模，受影响建议需重新采纳；无关建议沿用原采纳状态。
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    SUGGESTION_FRESHNESS_LABELS,
    SYNC_STATUS_LABELS,
    SuggestionFreshness,
    SyncStatus,
)
from src.models.column_profile import ColumnProfile
from src.models.model_run import ModelRun
from src.models.model_suggestion import ModelSuggestion
from src.models.relation import Relation
from src.models.source_table import SourceTable
from src.models.structure_sync import StructureSync
from src.services import profiling_service, star_service, sync_engine
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, local_now, utcnow


# ---------------------------------------------------------------- 快照与序列化

def snapshot_from_profiles(profiles: Sequence) -> list[dict]:
    return [{"column": p.column, "dtype": p.dtype} for p in profiles]


def _fallback_snapshot(db: Session, table: SourceTable) -> list[dict]:
    if table.structure_snapshot:
        return list(table.structure_snapshot)
    profiles = db.scalars(
        select(ColumnProfile).where(ColumnProfile.table_id == table.id).order_by(ColumnProfile.id)
    ).all()
    return snapshot_from_profiles(profiles)


def serialize_sync(sync: StructureSync, table: SourceTable | None = None) -> dict:
    return {
        "id": sync.id,
        "table_id": sync.table_id,
        "table_name": table.name if table else None,
        "table_cn_name": table.cn_name if table else None,
        "status": sync.status,
        "status_label": SYNC_STATUS_LABELS.get(sync.status, sync.status),
        "operator_email": sync.operator_email,
        "before_snapshot": sync.before_snapshot or [],
        "after_snapshot": sync.after_snapshot or [],
        "changes": sync.changes or [],
        "change_summary": sync_engine.summarize_changes(sync.changes or []),
        "affected_relation_ids": sync.affected_relation_ids or [],
        "affected_suggestion_ids": sync.affected_suggestion_ids or [],
        "rebuilt_run_id": sync.rebuilt_run_id,
        "error_message": sync.error_message,
        "confirmed_by": sync.confirmed_by,
        "confirmed_at": iso_utc(sync.confirmed_at),
        "created_at": iso_utc(sync.created_at),
    }


# ---------------------------------------------------------------- 查询

def list_syncs(db: Session, *, table_id: int | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(StructureSync)
    count_stmt = select(func.count()).select_from(StructureSync)
    if table_id:
        stmt = stmt.where(StructureSync.table_id == table_id)
        count_stmt = count_stmt.where(StructureSync.table_id == table_id)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(StructureSync.id.desc()).limit(limit).offset(offset)).all()
    tables = {t.id: t for t in db.scalars(select(SourceTable)).all()}
    return {
        "items": [serialize_sync(row, tables.get(row.table_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_sync(db: Session, sync_id: int) -> StructureSync:
    sync = db.get(StructureSync, sync_id)
    if sync is None:
        raise not_found("structure_sync", sync_id)
    return sync


def detail(db: Session, sync_id: int) -> dict:
    sync = get_sync(db, sync_id)
    table = db.get(SourceTable, sync.table_id)
    payload = serialize_sync(sync, table)
    # 受影响对象的可读明细，供管理员抽屉直接展示
    relations = {r.id: r for r in db.scalars(select(Relation)).all()}
    suggestions = {s.id: s for s in db.scalars(select(ModelSuggestion)).all()}
    tables = {t.id: t for t in db.scalars(select(SourceTable)).all()}
    payload["affected_relations"] = [
        {
            "id": rid,
            "left_table": tables.get(relations[rid].left_table_id).name if rid in relations and relations[rid].left_table_id in tables else None,
            "left_column": relations[rid].left_column if rid in relations else None,
            "right_table": tables.get(relations[rid].right_table_id).name if rid in relations and relations[rid].right_table_id in tables else None,
            "right_column": relations[rid].right_column if rid in relations else None,
        }
        for rid in (sync.affected_relation_ids or [])
    ]
    payload["affected_suggestions"] = [
        {
            "id": sid,
            "target_name": suggestions[sid].target_name if sid in suggestions else None,
            "freshness": suggestions[sid].freshness if sid in suggestions else None,
            "freshness_label": SUGGESTION_FRESHNESS_LABELS.get(
                suggestions[sid].freshness, suggestions[sid].freshness
            )
            if sid in suggestions
            else None,
            "stale_reason": suggestions[sid].stale_reason if sid in suggestions else None,
        }
        for sid in (sync.affected_suggestion_ids or [])
    ]
    return payload


# ---------------------------------------------------------------- 应用同步

def apply_sync(db: Session, table_id: int, payload: dict, operator_email: str) -> StructureSync:
    table = db.get(SourceTable, table_id)
    if table is None:
        raise not_found("source_table", table_id)

    before_snapshot = _fallback_snapshot(db, table)
    after_snapshot = [{"column": c["column"], "dtype": c["dtype"]} for c in payload["columns"]]
    after_names = [c["column"] for c in after_snapshot]
    if len(after_names) != len(set(after_names)):
        raise AppError("VALIDATION", 422, {"reason": "新结构存在重复列名"})

    changes = sync_engine.diff_structure(before_snapshot, after_snapshot, payload.get("changes"))
    stale_columns = sync_engine.invalidated_old_columns(changes)

    sync = StructureSync(
        table_id=table.id,
        status=SyncStatus.SUCCESS.value,
        operator_email=operator_email,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
        changes=changes,
    )
    db.add(sync)
    db.flush()  # 取得 sync.id，失败记录也需要主键

    try:
        # 结构改动放 SAVEPOINT：异常时回滚到同步前，旧版完整保留
        with db.begin_nested():
            affected_relation_ids: list[int] = []
            if stale_columns:
                relations = db.scalars(select(Relation)).all()
                for relation in relations:
                    if sync_engine.relation_is_affected(relation, table.id, stale_columns):
                        relation.freshness = SuggestionFreshness.STALE.value
                        relation.structure_sync_id = sync.id
                        affected_relation_ids.append(relation.id)

                suggestions = db.scalars(select(ModelSuggestion)).all()
                table_name_by_id = {t.id: t.name for t in db.scalars(select(SourceTable)).all()}
                affected_suggestion_ids: list[int] = []
                for suggestion in suggestions:
                    if suggestion.freshness == SuggestionFreshness.STALE.value:
                        continue
                    if sync_engine.suggestion_is_affected(
                        suggestion, table.id, stale_columns, table_name_by_id
                    ):
                        suggestion.freshness = SuggestionFreshness.STALE.value
                        suggestion.structure_sync_id = sync.id
                        suggestion.stale_reason = _stale_reason(changes)
                        affected_suggestion_ids.append(suggestion.id)
            else:
                affected_suggestion_ids = []

            # 落地新结构：抽样行（未提供则沿用现有）+ 重新剖析 + 快照 / 版本
            sample_rows = payload.get("sample_rows") or []
            if not sample_rows:
                sample_rows = table.sample_rows or []
            profiles = profiling_service.profile_table(sample_rows) if sample_rows else []
            db.execute(delete(ColumnProfile).where(ColumnProfile.table_id == table.id))
            for profile in profiles:
                db.add(ColumnProfile(table_id=table.id, **profile))
            table.sample_rows = sample_rows
            table.row_count = int(payload.get("row_count") or len(sample_rows))
            table.field_count = len(after_snapshot)
            table.structure_snapshot = after_snapshot
            table.structure_version = (table.structure_version or 1) + 1
            table.last_sync_at = utcnow()
            db.flush()

        sync.affected_relation_ids = affected_relation_ids
        sync.affected_suggestion_ids = affected_suggestion_ids
        db.flush()
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001 - 同步失败保留旧版，仅留痕
        # begin_nested 的 SAVEPOINT 已随异常自动回滚，结构改动全部撤销；
        # 外层事务里的 structure_syncs 失败记录仍然保留
        sync = db.get(StructureSync, sync.id)
        sync.status = SyncStatus.FAILED.value
        sync.error_message = str(exc)[:500]
        db.flush()
        raise AppError("SYNC_FAILED", 409, {"sync_id": sync.id, "reason": str(exc)[:200]})
    return sync


def _stale_reason(changes: list[dict]) -> str:
    parts = [
        f"{c['change_kind']}:{c.get('old_name') or c['column']}"
        for c in changes
        if c["change_kind"] in sync_engine.INVALIDATING_CHANGE_KINDS
    ]
    return "依据字段已变化（" + "，".join(parts[:6]) + "），需按新结构重建后重新采纳"


# ---------------------------------------------------------------- 确认重建

def _next_run_no(db: Session) -> str:
    prefix = f"MR{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(ModelRun).where(ModelRun.run_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def rebuild(db: Session, sync: StructureSync, actor_email: str) -> tuple[StructureSync, ModelRun]:
    """管理员确认后按新结构重建：受影响表重新建模并需重新采纳，无关建议保持可用。"""
    if sync.status == SyncStatus.FAILED.value:
        raise AppError("CONFLICT", 409, {"reason": "失败的同步没有生效，无需重建；请修正后重新发起同步"})
    if sync.status == SyncStatus.REBUILT.value:
        raise AppError("CONFLICT", 409, {"reason": "该同步已确认重建，请勿重复操作"})

    table = db.get(SourceTable, sync.table_id)
    if table is None:
        raise not_found("source_table", sync.table_id)

    affected_suggestion_ids = set(sync.affected_suggestion_ids or [])
    affected_table_ids = {
        sid_table[0]
        for sid_table in db.execute(
            select(ModelSuggestion.table_id).where(ModelSuggestion.id.in_(affected_suggestion_ids or [-1]))
        ).all()
    }
    affected_table_ids.add(table.id)

    run = ModelRun(
        run_no=_next_run_no(db),
        trigger="sync",
        status="running",
        operator_email=actor_email,
    )
    db.add(run)
    db.flush()

    # 1) 重新剖析同步表（其它表剖析结果沿用）
    profiles = profiling_service.profile_table(table.sample_rows or [])
    db.execute(delete(ColumnProfile).where(ColumnProfile.table_id == table.id))
    for profile in profiles:
        db.add(ColumnProfile(table_id=table.id, **profile))
    db.flush()

    # 2) 全局关系发现（新关系全部 current，旧 stale 边被取代）
    all_tables = db.scalars(select(SourceTable).order_by(SourceTable.id)).all()
    table_dicts = [
        {
            "id": t.id,
            "name": t.name,
            "cn_name": t.cn_name,
            "subject": t.subject,
            "domain": t.domain,
            "row_count": t.row_count,
            "sample_rows": t.sample_rows or [],
        }
        for t in all_tables
    ]
    profiling = profiling_service.profile_tables(table_dicts)
    profile_rows: list[dict] = []
    for t_dict in table_dicts:
        profile_rows.extend(
            {"table_id": t_dict["id"], **profile} for profile in profiling["profiles"][t_dict["id"]]
        )
    discovery = profiling_service.discover_relations(profile_rows)
    db.execute(delete(Relation))
    for relation in discovery["relations"]:
        db.add(Relation(**relation))
    db.flush()

    # 3) 重建星型建议
    rows_by_table = {t.id: (t.sample_rows or []) for t in all_tables}
    built = star_service.build_suggestions(
        table_dicts, profile_rows, discovery["relations"], rows_by_table
    )

    # 4) 旧建议映射：受影响表强制重新采纳；无关表沿用原采纳状态
    previous_run_id = db.scalar(
        select(func.max(ModelRun.id)).where(ModelRun.id != run.id)
    )
    previous_by_table: dict[int, ModelSuggestion] = {}
    if previous_run_id:
        for old in db.scalars(
            select(ModelSuggestion).where(ModelSuggestion.run_id == previous_run_id)
        ).all():
            previous_by_table[old.table_id] = old

    new_ids: list[int] = []
    for suggestion in built["suggestions"]:
        previous = previous_by_table.get(suggestion["table_id"])
        is_affected = suggestion["table_id"] in affected_table_ids
        status = "pending"
        adopted_by = None
        adopted_at = None
        if not is_affected and previous is not None:
            # 无关建议保持可用：沿用原人工结论
            status = previous.status
            adopted_by = previous.adopted_by
            adopted_at = previous.adopted_at
        entity = ModelSuggestion(
            run_id=run.id,
            table_id=suggestion["table_id"],
            target_name=suggestion["target_name"],
            role=suggestion["role"],
            grain=suggestion["grain"],
            joins=suggestion["joins"],
            measures=suggestion["measures"],
            attributes=suggestion["attributes"],
            confidence=suggestion["confidence"],
            status=status,
            freshness=SuggestionFreshness.CURRENT.value,
            ddl=suggestion["ddl"],
            reason=suggestion["reason"],
            adopted_by=adopted_by,
            adopted_at=adopted_at,
        )
        db.add(entity)
        db.flush()
        new_ids.append(entity.id)
        if is_affected and previous is not None:
            previous.superseded_by_id = entity.id

    # 5) 收口运行与同步记录
    run.table_count = len(table_dicts)
    run.profile_count = profiling["total"]
    run.relation_count = len(discovery["relations"])
    run.suggestion_count = len(built["suggestions"])
    run.status = "success"
    run.metrics = {
        "structure_sync_id": sync.id,
        "affected_tables": sorted(affected_table_ids),
        "rebuilt_suggestions": new_ids,
        "relations": len(discovery["relations"]),
    }
    run.log_text = (
        f"结构同步 #{sync.id} 确认重建：重新剖析 {table.name}，"
        f"全局发现 {len(discovery['relations'])} 条关系，生成 {len(built['suggestions'])} 条建议；"
        f"受影响表 {len(affected_table_ids)} 张需重新采纳，无关建议沿用原采纳状态。"
    )
    run.finished_at = utcnow()

    sync.status = SyncStatus.REBUILT.value
    sync.rebuilt_run_id = run.id
    sync.confirmed_by = actor_email
    sync.confirmed_at = utcnow()
    db.flush()
    return sync, run
