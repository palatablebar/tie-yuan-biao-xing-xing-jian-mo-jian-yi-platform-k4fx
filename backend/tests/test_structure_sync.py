"""结构同步：迁移规则 diff、影响隔离、失效导出、失败回滚、确认重建的端到端单测。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.models import Base
from src.models.column_profile import ColumnProfile
from src.models.model_run import ModelRun
from src.models.model_suggestion import ModelSuggestion
from src.models.relation import Relation
from src.models.source_table import SourceTable
from src.models.structure_sync import StructureSync
from src.services import (
    export_service,
    model_run_service,
    structure_sync_service,
    sync_engine,
)
from src.utils.errors import AppError


# ---------------------------------------------------------------- 纯函数：迁移规则 diff

def test_diff_detects_four_change_kinds():
    before = [
        {"column": "a_id", "dtype": "int"},
        {"column": "region_id", "dtype": "int"},
        {"column": "amount", "dtype": "decimal"},
        {"column": "keep_me", "dtype": "string"},
    ]
    after = [
        {"column": "keep_me", "dtype": "string"},
        {"column": "region_id", "dtype": "int"},  # 占位，下面改名会把它替换
    ]
    after = [
        {"column": "keep_me", "dtype": "string"},
        {"column": "zone_id", "dtype": "int"},          # region_id 改名而来
        {"column": "amount", "dtype": "string"},        # 类型变更
        {"column": "remark", "dtype": "string"},        # 新增
    ]
    changes = sync_engine.diff_structure(
        before, after, [{"change_kind": "renamed", "old_name": "region_id", "column": "zone_id"}]
    )
    kinds = {(c["change_kind"], c["column"]) for c in changes}
    assert ("removed", "a_id") in kinds
    assert ("renamed", "zone_id") in kinds
    assert ("type_changed", "amount") in kinds
    assert ("added", "remark") in kinds
    assert all(c["column"] != "keep_me" for c in changes)  # 未变化列不产生规则
    # 固定排序：added → removed → renamed → type_changed
    order = ["added", "removed", "renamed", "type_changed"]
    assert [c["change_kind"] for c in changes] == sorted(
        (c["change_kind"] for c in changes), key=order.index
    )


def test_invalidating_columns_exclude_added():
    changes = [
        {"change_kind": "added", "column": "remark", "old_name": None},
        {"change_kind": "removed", "column": "a_id", "old_name": None},
        {"change_kind": "renamed", "column": "zone_id", "old_name": "region_id"},
        {"change_kind": "type_changed", "column": "amount", "old_name": None},
    ]
    stale = sync_engine.invalidated_old_columns(changes)
    assert stale == {"a_id", "region_id", "amount"}  # 新增列不影响既有结论


def test_rename_validation_requires_real_columns():
    before = [{"column": "a_id", "dtype": "int"}]
    after = [{"column": "b_id", "dtype": "int"}]
    with pytest.raises(AppError) as exc:
        sync_engine.diff_structure(
            before, after, [{"change_kind": "renamed", "old_name": "ghost", "column": "b_id"}]
        )
    assert exc.value.status_code == 422


# ---------------------------------------------------------------- 端到端生命周期

class _Factory:
    def __init__(self, db: Session):
        self.db = db

    def source(self, name, cn, subject, rows, row_count):
        table = SourceTable(
            name=name, cn_name=cn, subject=subject, domain="sales",
            row_count=row_count, sample_rows=rows, field_count=len(rows[0]),
            structure_snapshot=[{"column": k, "dtype": ""} for k in rows[0]],
            structure_version=1,
        )
        self.db.add(table)
        self.db.flush()
        return table


def _dim_rows(pk, names=("甲", "乙", "丙", "丁")):
    return [{pk: i + 1, "name": f"{n}{i+1}", "code": f"C{i+1}"} for i, n in enumerate(names)]


def _fact_rows():
    return [
        {"order_item_id": 100 + i, "a_id": 1 + i % 4, "b_id": 1 + i % 4, "c_id": 1 + i % 4,
         "region_id": 1 + i % 5, "amount": 10.5 + i, "qty": i + 1}
        for i in range(8)
    ]


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _bootstrap(db: Session):
    factory = _Factory(db)
    dim_a = factory.source("ods_dim_a", "维度A", "a", _dim_rows("a_id"), 4)
    dim_b = factory.source("ods_dim_b", "维度B", "b", _dim_rows("b_id"), 4)
    dim_c = factory.source("ods_dim_c", "维度C", "c", _dim_rows("c_id"), 4)
    fact = factory.source("ods_fact", "事实表", "sales", _fact_rows(), 200_000)
    run = model_run_service.create_and_execute(db, trigger="manual", operator_email="admin@starmodel.io")
    db.flush()
    return factory, {"dim_a": dim_a, "dim_b": dim_b, "dim_c": dim_c, "fact": fact}, run


def _suggestions_by_table(db: Session, run_id: int) -> dict[int, ModelSuggestion]:
    rows = db.scalars(select(ModelSuggestion).where(ModelSuggestion.run_id == run_id)).all()
    return {row.table_id: row for row in rows}


def test_sync_lifecycle_stale_isolation_rebuild_and_export_guard(db):
    _factory, tables, run = _bootstrap(db)
    fact, dim_a, dim_b, dim_c = tables["fact"], tables["dim_a"], tables["dim_b"], tables["dim_c"]

    # 初始：3 条关系、4 条建议，全部 current
    assert db.scalar(select(func.count()).select_from(Relation)) == 3
    before = _suggestions_by_table(db, run.id)
    assert set(before) == {fact.id, dim_a.id, dim_b.id, dim_c.id}
    assert all(s.freshness == "current" for s in before.values())

    # 无关建议（dim_b）先采纳，重建后应保持可用
    suggestion_service = _adopt(db, before[dim_b.id], "admin@starmodel.io")

    # 管理员发起结构同步：删除 a_id、region_id 改名 zone_id、amount 改类型、新增 remark
    after_rows = [
        {"order_item_id": r["order_item_id"], "b_id": r["b_id"], "c_id": r["c_id"],
         "zone_id": r["region_id"], "amount": str(r["amount"]), "qty": r["qty"], "remark": "x"}
        for r in _fact_rows()
    ]
    payload = {
        "columns": [
            {"column": "order_item_id", "dtype": "int"},
            {"column": "b_id", "dtype": "int"},
            {"column": "c_id", "dtype": "int"},
            {"column": "zone_id", "dtype": "int"},
            {"column": "amount", "dtype": "string"},
            {"column": "qty", "dtype": "int"},
            {"column": "remark", "dtype": "string"},
        ],
        "sample_rows": after_rows,
        "row_count": 200_000,
        "changes": [{"change_kind": "renamed", "old_name": "region_id", "column": "zone_id"}],
    }
    sync = structure_sync_service.apply_sync(db, fact.id, payload, "admin@starmodel.io")
    db.flush()

    # 同步留痕且可追溯：变化前后字段 + 迁移规则齐全
    assert sync.status == "success"
    kinds = {c["change_kind"] for c in sync.changes}
    assert {"removed", "renamed", "type_changed", "added"} <= kinds
    assert len(sync.before_snapshot) == 7 and len(sync.after_snapshot) == 7
    assert fact.structure_version == 2

    # 影响隔离：只有引用 a_id 的边失效；b_id / c_id 边保持 current
    stale_rels = db.scalars(select(Relation).where(Relation.freshness == "stale")).all()
    assert len(stale_rels) == 1
    assert stale_rels[0].left_column == "a_id"
    assert set(sync.affected_relation_ids) == {stale_rels[0].id}
    current_rels = db.scalars(select(Relation).where(Relation.freshness == "current")).all()
    assert {r.left_column for r in current_rels} == {"b_id", "c_id"}

    # 建议隔离：事实表与维度A（跨表传导）失效；维度B/C 保持可用
    stale_sug = db.scalars(select(ModelSuggestion).where(ModelSuggestion.freshness == "stale")).all()
    assert {s.table_id for s in stale_sug} == {fact.id, dim_a.id}
    assert all(s.stale_reason for s in stale_sug)
    fresh = db.scalars(select(ModelSuggestion).where(ModelSuggestion.freshness == "current")).all()
    assert {s.table_id for s in fresh} == {dim_b.id, dim_c.id}

    # 字段变化后导出资格立即失效：DDL 导出被拒绝
    with pytest.raises(AppError) as exc:
        export_service.ddl_sql(db)
    assert exc.value.code == "EXPORT_STALE"
    assert exc.value.status_code == 409

    # 过期建议不能采纳
    with pytest.raises(AppError) as adopt_exc:
        suggestion_service.adopt(db, before[fact.id], "admin@starmodel.io")
    assert adopt_exc.value.status_code == 409

    # 管理员确认按新结构重建
    sync, new_run = structure_sync_service.rebuild(db, sync, "admin@starmodel.io")
    db.flush()
    assert new_run.trigger == "sync" and new_run.status == "success"
    assert sync.status == "rebuilt" and sync.rebuilt_run_id == new_run.id and sync.confirmed_by

    # 新建议全部 current；受影响事实表需重新采纳（pending）
    after_map = _suggestions_by_table(db, new_run.id)
    assert all(s.freshness == "current" for s in after_map.values())
    assert after_map[fact.id].status == "pending"
    # 无关建议保持可用：dim_b 的采纳结论被沿用
    assert after_map[dim_b.id].status == "adopted"
    assert after_map[dim_b.id].adopted_by == "admin@starmodel.io"

    # 旧事实建议被新建议取代（留档可追溯）
    db.refresh(before[fact.id])
    assert before[fact.id].freshness == "stale"
    assert before[fact.id].superseded_by_id == after_map[fact.id].id

    # 重建后导出恢复
    result = export_service.ddl_sql(db)
    assert result["segments"] >= 1 and "CREATE TABLE" in result["sql"]


def test_failed_sync_keeps_previous_version(db, monkeypatch):
    _factory, tables, run = _bootstrap(db)
    fact = tables["fact"]
    original_rows = [dict(r) for r in fact.sample_rows]
    original_relation_count = db.scalar(select(func.count()).select_from(Relation))
    original_profile_count = db.scalar(select(func.count()).select_from(ColumnProfile))

    def boom(*_args, **_kwargs):
        raise RuntimeError("upstream metadata service unreachable")

    monkeypatch.setattr(structure_sync_service.profiling_service, "profile_table", boom)

    payload = {
        "columns": [{"column": "only_col", "dtype": "int"}],
        "sample_rows": [{"only_col": 1}],
        "row_count": 1,
        "changes": [],
    }
    with pytest.raises(AppError) as exc:
        structure_sync_service.apply_sync(db, fact.id, payload, "admin@starmodel.io")
    assert exc.value.code == "SYNC_FAILED"
    db.flush()

    # 失败留痕，但旧版结构 / 关系 / 剖析完整保留
    failed = db.scalars(select(StructureSync).where(StructureSync.status == "failed")).all()
    assert len(failed) == 1 and failed[0].error_message
    db.refresh(fact)
    assert fact.sample_rows == original_rows
    assert fact.structure_version == 1
    assert db.scalar(select(func.count()).select_from(Relation)) == original_relation_count
    assert db.scalar(select(func.count()).select_from(ColumnProfile)) == original_profile_count
    assert db.scalar(select(func.count()).select_from(ModelSuggestion).where(ModelSuggestion.freshness == "stale")) == 0


def test_added_only_column_does_not_invalidate(db):
    _factory, tables, run = _bootstrap(db)
    fact = tables["fact"]
    after_rows = [dict(r, remark="n/a") for r in _fact_rows()]
    # 已存在列沿用同步前快照里的真实类型，仅新增 remark，确保只产生 added 规则
    payload = {
        "columns": list(fact.structure_snapshot) + [{"column": "remark", "dtype": "string"}],
        "sample_rows": after_rows,
        "row_count": 200_000,
        "changes": [],
    }
    sync = structure_sync_service.apply_sync(db, fact.id, payload, "admin@starmodel.io")
    db.flush()
    assert [c["change_kind"] for c in sync.changes] == ["added"]
    assert sync.affected_relation_ids == [] and sync.affected_suggestion_ids == []
    assert db.scalar(select(func.count()).select_from(ModelSuggestion).where(ModelSuggestion.freshness == "stale")) == 0
    # 无关变化下导出资格保持
    assert export_service.ddl_sql(db)["segments"] >= 1


def _adopt(db: Session, suggestion: ModelSuggestion, email: str):
    from src.services import suggestion_service

    suggestion_service.adopt(db, suggestion, email)
    db.flush()
    return suggestion_service


def test_empty_sample_rows_preserves_existing_samples(db):
    _factory, tables, run = _bootstrap(db)
    fact = tables["fact"]
    original_rows = [dict(r) for r in fact.sample_rows]
    # 仅声明新增列、不提供抽样行：后端应沿用现有抽样行而不是清空
    payload = {
        "columns": list(fact.structure_snapshot) + [{"column": "remark", "dtype": "string"}],
        "sample_rows": [],
        "row_count": 0,
        "changes": [],
    }
    sync = structure_sync_service.apply_sync(db, fact.id, payload, "admin@starmodel.io")
    db.flush()
    assert sync.status == "success"
    db.refresh(fact)
    assert fact.sample_rows == original_rows  # 抽样行保留
    assert fact.row_count == len(original_rows)  # row_count=0 回退为抽样行数
    # 现有列剖析仍在（按保留的抽样行重算；新增的 remark 在抽样行里没有数据故不产生剖析）
    profile_count = db.scalar(
        select(func.count())
        .select_from(ColumnProfile)
        .where(ColumnProfile.table_id == fact.id)
    )
    assert profile_count == len(original_rows[0])
    assert any(c["column"] == "remark" for c in fact.structure_snapshot)  # 结构快照含声明的新列
