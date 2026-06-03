"""
Blocking reasons router — MOD-6.

Spec: moderation/openapi.yaml
  GET    /api/v1/blocking-reasons              — list (filter: hard_block, is_active)
  POST   /api/v1/blocking-reasons              — create (admin)
  PATCH  /api/v1/blocking-reasons/{reason_id}  — update (admin)
  DELETE /api/v1/blocking-reasons/{reason_id}  — soft-deactivate (admin)

ADR (storage):
  DB table chosen over enum-in-code or i18n-catalog.
  Criteria:
    - New reason without migration: INSERT into table vs code change + migration for enum.
    - Historical references: soft-delete (is_active=False) keeps UUID in FK-safe state;
      physical delete would break blocking_reason_ids JSON in product_moderation.
    - Multilanguage: column per lang is future work; table supports it without schema changes.
  Trade-off: requires admin CRUD instead of just code deploy, but that is acceptable for
  a moderation platform where reasons are managed by ops, not developers.

Auth:
  GET   — JWT (any moderator can read the list)
  POST/PATCH/DELETE — JWT (admin role enforcement is future work; all moderators for MVP)

Referenced-reason deletion:
  Spec: DELETE = deactivate (204). Physical delete is never done.
  A reason referenced in product_moderation.blocking_reason_ids (JSON array) stays
  referenced safely because UUIDs in JSON are not FK-constrained. is_active=False
  hides the reason from the list while preserving historical audit trails.
"""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_moderator_id
from backend.database import get_db
from backend.modules.moderation.models import BlockingReason
from backend.modules.blocking_reasons.schemas import (
    BlockingReasonCreateRequest,
    BlockingReasonResponse,
    BlockingReasonUpdateRequest,
)

router = APIRouter(prefix="/api/v1", tags=["BlockingReasons"])


@router.get(
    "/blocking-reasons",
    response_model=List[BlockingReasonResponse],
    summary="Справочник причин блокировки (MOD-6)",
)
async def list_blocking_reasons(
    hard_block: Optional[bool] = None,
    is_active: Optional[bool] = True,
    _: UUID = Depends(get_current_moderator_id),
    db: AsyncSession = Depends(get_db),
) -> List[BlockingReasonResponse]:
    """
    spec moderation/openapi.yaml GET /api/v1/blocking-reasons

    Returns active blocking reasons by default (is_active=true).
    Optional filter: hard_block=true|false.
    """
    conditions = []
    if is_active is not None:
        conditions.append(BlockingReason.is_active == is_active)
    if hard_block is not None:
        conditions.append(BlockingReason.hard_block == hard_block)

    stmt = select(BlockingReason).order_by(BlockingReason.created_at.asc())
    if conditions:
        stmt = stmt.where(*conditions)

    result = await db.execute(stmt)
    return list(result.scalars())


@router.post(
    "/blocking-reasons",
    response_model=BlockingReasonResponse,
    status_code=201,
    summary="Создать причину блокировки (admin)",
)
async def create_blocking_reason(
    body: BlockingReasonCreateRequest,
    _: UUID = Depends(get_current_moderator_id),
    db: AsyncSession = Depends(get_db),
) -> BlockingReasonResponse:
    """
    spec moderation/openapi.yaml POST /api/v1/blocking-reasons
    code must be unique and match [A-Z_]+.
    """
    existing = await db.execute(
        select(BlockingReason).where(BlockingReason.code == body.code)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "CODE_EXISTS", "message": f"Blocking reason with code '{body.code}' already exists"},
        )

    reason = BlockingReason(
        code=body.code,
        title=body.title,
        description=body.description,
        hard_block=body.hard_block,
        is_active=True,
    )
    db.add(reason)
    await db.commit()
    await db.refresh(reason)
    return reason


@router.patch(
    "/blocking-reasons/{reason_id}",
    response_model=BlockingReasonResponse,
    summary="Обновить причину блокировки (admin)",
)
async def update_blocking_reason(
    reason_id: UUID,
    body: BlockingReasonUpdateRequest,
    _: UUID = Depends(get_current_moderator_id),
    db: AsyncSession = Depends(get_db),
) -> BlockingReasonResponse:
    """spec moderation/openapi.yaml PATCH /api/v1/blocking-reasons/{reason_id}"""
    result = await db.execute(
        select(BlockingReason).where(BlockingReason.id == reason_id)
    )
    reason = result.scalar_one_or_none()
    if reason is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Blocking reason not found"},
        )

    if body.title is not None:
        reason.title = body.title
    if body.description is not None:
        reason.description = body.description
    if body.is_active is not None:
        reason.is_active = body.is_active

    await db.commit()
    await db.refresh(reason)
    return reason


@router.delete(
    "/blocking-reasons/{reason_id}",
    status_code=204,
    summary="Деактивировать причину блокировки (admin)",
)
async def deactivate_blocking_reason(
    reason_id: UUID,
    _: UUID = Depends(get_current_moderator_id),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    spec moderation/openapi.yaml DELETE /api/v1/blocking-reasons/{reason_id}

    Soft-deactivation: sets is_active=False. Never physically deleted.
    Historical blocking_reason_ids in product_moderation remain intact.
    """
    result = await db.execute(
        select(BlockingReason).where(BlockingReason.id == reason_id)
    )
    reason = result.scalar_one_or_none()
    if reason is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Blocking reason not found"},
        )

    reason.is_active = False
    await db.commit()
    return Response(status_code=204)
