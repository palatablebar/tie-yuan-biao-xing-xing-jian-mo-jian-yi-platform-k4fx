from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import READ_ROLES, SYNC_WRITE_ROLES
from src.controllers import structure_sync_controller
from src.middlewares.rbac import require_roles
from src.schemas.structure_sync import StructureSyncIn
from src.utils.pagination import page_params

router = APIRouter(tags=["structure-sync"])


@router.get("/structure-syncs")
def list_syncs(
    table_id: int | None = None,
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return structure_sync_controller.list_syncs(
        db, table_id=table_id, limit=page.limit, offset=page.offset
    )


@router.get("/structure-syncs/{sync_id}")
def get_sync(
    sync_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return structure_sync_controller.detail(db, sync_id)


@router.post("/source-tables/{table_id}/structure-sync", status_code=201)
def apply_sync(
    table_id: int,
    payload: StructureSyncIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*SYNC_WRITE_ROLES)),
) -> dict:
    return structure_sync_controller.apply_sync(db, table_id, payload.model_dump(), principal, request)


@router.post("/structure-syncs/{sync_id}/rebuild")
def confirm_rebuild(
    sync_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*SYNC_WRITE_ROLES)),
) -> dict:
    return structure_sync_controller.confirm_rebuild(db, sync_id, principal, request)
