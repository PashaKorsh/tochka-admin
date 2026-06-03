"""
Queue router — MOD-2 get-next-card flow.

Spec: moderation/openapi.yaml POST /api/v1/queue/claim
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_moderator_id
from backend.database import get_db
from backend.modules.queue.schemas import ClaimRequest, TicketResponse
from backend.modules.queue.service import QueueService

router = APIRouter(prefix="/api/v1", tags=["Queue"])


@router.post(
    "/queue/claim",
    response_model=TicketResponse,
    summary="Взять следующий тикет в работу",
)
async def claim_next(
    request: Optional[ClaimRequest] = None,
    moderator_id=Depends(get_current_moderator_id),
    db: AsyncSession = Depends(get_db),
):
    """
    spec moderation/openapi.yaml POST /api/v1/queue/claim

    Atomically claim the next PENDING ticket (MOD-2 flow).
    TTL 30 minutes; auto-return to PENDING if claim_expires_at < now.
    """
    queue_priority = request.queue_priority if request else None
    category_ids = request.category_ids if request else None

    try:
        ticket = await QueueService.claim_next(
            db,
            moderator_id=moderator_id,
            queue_priority=queue_priority,
            category_ids=category_ids,
        )
    except ValueError as exc:
        if str(exc) == "MODERATOR_ALREADY_IN_REVIEW":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "MODERATOR_ALREADY_IN_REVIEW",
                    "message": "Moderator already has an active ticket in review",
                },
            )
        raise

    if ticket is None:
        return Response(status_code=204)

    return ticket
