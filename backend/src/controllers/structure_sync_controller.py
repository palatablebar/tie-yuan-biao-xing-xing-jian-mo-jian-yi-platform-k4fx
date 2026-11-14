"""结构同步控制器：同步 / 确认重建 / 查询，全部写领域审计。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, structure_sync_service
from src.utils.errors import AppError


def list_syncs(db: Session, **filters) -> dict:
    return structure_sync_service.list_syncs(db, **filters)


def detail(db: Session, sync_id: int) -> dict:
    return structure_sync_service.detail(db, sync_id)


def apply_sync(db: Session, table_id: int, payload: dict, principal: dict, request: Request) -> dict:
    try:
        sync = structure_sync_service.apply_sync(db, table_id, payload, principal["email"])
    except AppError as exc:
        if exc.code == "SYNC_FAILED":
            audit_service.record(
                db,
                actor_email=principal["email"],
                actor_role=principal["role"],
                action="structure_sync.fail",
                entity="structure_sync",
                entity_id=exc.detail.get("sync_id"),
                detail={"table_id": table_id, "reason": exc.detail.get("reason")},
                ip=request.client.host if request.client else None,
            )
            db.commit()
        raise
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="structure_sync.apply",
        entity="structure_sync",
        entity_id=sync.id,
        detail={
            "table_id": table_id,
            "changes": structure_sync_service.serialize_sync(sync)["change_summary"],
            "relations": len(sync.affected_relation_ids or []),
            "suggestions": len(sync.affected_suggestion_ids or []),
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return structure_sync_service.detail(db, sync.id)


def confirm_rebuild(db: Session, sync_id: int, principal: dict, request: Request) -> dict:
    sync = structure_sync_service.get_sync(db, sync_id)
    sync, run = structure_sync_service.rebuild(db, sync, principal["email"])
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="structure_sync.rebuild",
        entity="structure_sync",
        entity_id=sync.id,
        detail={
            "run_id": run.id,
            "run_no": run.run_no,
            "relations": run.relation_count,
            "suggestions": run.suggestion_count,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return structure_sync_service.detail(db, sync.id)
