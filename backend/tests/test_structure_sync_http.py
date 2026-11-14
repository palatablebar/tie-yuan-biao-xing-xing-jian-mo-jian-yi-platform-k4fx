"""结构同步 HTTP 集成：RBAC、同步留痕、导出拒绝、确认重建（FastAPI TestClient + SQLite）。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.config.database import get_db
from src.main import app
from src.middlewares import audit_logger
from src.models import Base
from src.models.model_suggestion import ModelSuggestion
from src.models.source_table import SourceTable
from src.models.structure_sync import StructureSync
from src.services import model_run_service
from src.utils.security import create_access_token


@pytest.fixture()
def client_db(tmp_path):
    # 文件库：请求处理与审计中间件各自开独立 Session，通过提交共享数据，贴近生产语义
    engine = create_engine(f"sqlite:///{tmp_path / 'sync_test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    seed_session = factory()

    def _get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    audit_logger.SessionLocal = factory

    # 不用 with 触发 lifespan（lifespan 会 wait_for_db 连真实库并 seed）；中间件与路由仍然生效
    client = TestClient(app)
    try:
        yield client, seed_session
    finally:
        app.dependency_overrides.clear()
        seed_session.close()
        engine.dispose()


def _token(role: str) -> str:
    email = {"admin": "admin@starmodel.io", "analyst": "analyst@starmodel.io", "viewer": "viewer@starmodel.io"}[role]
    token, _ = create_access_token(email, role)
    return token


def _auth(role: str) -> dict:
    return {"Authorization": f"Bearer {_token(role)}"}


def _seed(db: Session) -> SourceTable:
    dim_a_rows = [{"a_id": i, "a_name": f"维A{i}", "code": f"A{i}"} for i in range(1, 5)]
    dim_b_rows = [{"b_id": i, "b_name": f"维B{i}", "code": f"B{i}"} for i in range(1, 5)]
    fact_rows = [
        {"order_item_id": 100 + i, "a_id": 1 + i % 4, "b_id": 1 + i % 4, "amount": 10.0 + i, "qty": i + 1}
        for i in range(6)
    ]

    def snap(rows, dtypes):
        return [{"column": k, "dtype": dtypes.get(k, "string")} for k in rows[0]]

    fact_dtypes = {"order_item_id": "int", "a_id": "int", "b_id": "int", "amount": "decimal", "qty": "int"}
    dim_a = SourceTable(
        name="ods_dim_a", cn_name="维度A", subject="a", domain="sales", row_count=4,
        sample_rows=dim_a_rows, field_count=3,
        structure_snapshot=snap(dim_a_rows, {"a_id": "int"}), structure_version=1,
    )
    dim_b = SourceTable(
        name="ods_dim_b", cn_name="维度B", subject="b", domain="sales", row_count=4,
        sample_rows=dim_b_rows, field_count=3,
        structure_snapshot=snap(dim_b_rows, {"b_id": "int"}), structure_version=1,
    )
    fact = SourceTable(
        name="ods_fact", cn_name="事实", subject="sales", domain="sales", row_count=200_000,
        sample_rows=fact_rows, field_count=5,
        structure_snapshot=snap(fact_rows, fact_dtypes), structure_version=1,
    )
    db.add_all([dim_a, dim_b, fact])
    db.flush()
    model_run_service.create_and_execute(db, trigger="manual", operator_email="admin@starmodel.io")
    db.commit()
    return fact


def test_rbac_analyst_cannot_sync_but_can_read(client_db):
    client, db = client_db
    fact = _seed(db)
    body = {
        "columns": [{"column": "order_item_id", "dtype": "int"}, {"column": "amount", "dtype": "decimal"}],
        "sample_rows": [{"order_item_id": 1, "amount": 1.0}],
        "row_count": 1,
        "changes": [],
    }
    # 未登录 401
    assert client.post(f"/api/source-tables/{fact.id}/structure-sync", json=body).status_code == 401
    # 专员无结构同步写权限 403
    forbidden = client.post(
        f"/api/source-tables/{fact.id}/structure-sync", json=body, headers=_auth("analyst")
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "FORBIDDEN"
    # 只读访客可查同步列表（此时为空）
    listing = client.get("/api/structure-syncs", headers=_auth("viewer"))
    assert listing.status_code == 200 and listing.json()["total"] == 0


def test_sync_invalidates_and_blocks_export_then_rebuild_restores(client_db):
    client, db = client_db
    fact = _seed(db)

    # 正常导出先可用
    assert client.get("/api/exports/ddl.sql", headers=_auth("admin")).status_code == 200

    # 改度量列 amount 类型（decimal→string）：事实建议立即失效，但外键边保留，
    # 重建后仍能重新产出事实 + 维度建议（导出可恢复）
    after_rows = [
        {
            "order_item_id": r["order_item_id"],
            "a_id": r["a_id"],
            "b_id": r["b_id"],
            "amount": str(r["amount"]),
            "qty": r["qty"],
        }
        for r in fact.sample_rows
    ]
    body = {
        "columns": [
            {"column": "order_item_id", "dtype": "int"},
            {"column": "a_id", "dtype": "int"},
            {"column": "b_id", "dtype": "int"},
            {"column": "amount", "dtype": "string"},
            {"column": "qty", "dtype": "int"},
        ],
        "sample_rows": after_rows,
        "row_count": 200_000,
        "changes": [],
    }
    resp = client.post(f"/api/source-tables/{fact.id}/structure-sync", json=body, headers=_auth("admin"))
    assert resp.status_code == 201, resp.text
    sync = resp.json()
    assert sync["status"] == "success"
    assert sync["change_summary"]["type_changed"] >= 1
    assert len(sync["affected_suggestion_ids"]) >= 1

    # 建议列表携带 freshness，过期建议 exportable=false
    listing = client.get("/api/model-suggestions", headers=_auth("admin")).json()
    stale_items = [item for item in listing["items"] if item["freshness"] == "stale"]
    assert stale_items and all(item["exportable"] is False for item in stale_items)

    # 导出立即被拒绝（真实 409 + 业务码 EXPORT_STALE）
    blocked = client.get("/api/exports/ddl.sql", headers=_auth("admin"))
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "EXPORT_STALE"

    # 管理员确认重建
    rebuilt = client.post(f"/api/structure-syncs/{sync['id']}/rebuild", headers=_auth("admin"))
    assert rebuilt.status_code == 200, rebuilt.text
    assert rebuilt.json()["status"] == "rebuilt"

    # 重建后导出恢复
    assert client.get("/api/exports/ddl.sql", headers=_auth("admin")).status_code == 200
    db.refresh(fact)
    assert fact.structure_version == 2

    # 每次同步都可追溯：含变化前后字段
    record = db.scalar(select(StructureSync).where(StructureSync.id == sync["id"]))
    assert record.before_snapshot and record.after_snapshot and record.confirmed_by
    assert db.scalar(
        select(ModelSuggestion).where(ModelSuggestion.freshness == "stale")
    ) is not None  # 旧建议留档


def test_failed_sync_returns_409_and_keeps_old_version(client_db, monkeypatch):
    client, db = client_db
    fact = _seed(db)
    from src.services import structure_sync_service

    def boom(*_a, **_k):
        raise RuntimeError("metadata api 500")

    monkeypatch.setattr(structure_sync_service.profiling_service, "profile_table", boom)
    body = {
        "columns": [{"column": "x", "dtype": "int"}],
        "sample_rows": [{"x": 1}],
        "row_count": 1,
        "changes": [],
    }
    resp = client.post(f"/api/source-tables/{fact.id}/structure-sync", json=body, headers=_auth("admin"))
    assert resp.status_code == 409
    assert resp.json()["code"] == "SYNC_FAILED"
    db.refresh(fact)
    assert fact.structure_version == 1  # 旧版保留
    failed = db.scalar(select(StructureSync).where(StructureSync.status == "failed"))
    assert failed is not None and failed.error_message
