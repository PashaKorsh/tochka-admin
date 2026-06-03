"""
Tickets router — MOD-3 approve + MOD-4 soft-block.

Spec: moderation/openapi.yaml
  POST /api/v1/tickets/{ticket_id}/approve → 200 TicketResponse | 403 | 404 | 409 | 502
  POST /api/v1/tickets/{ticket_id}/block   → 200 TicketResponse | 400 | 403 | 404 | 409 | 502
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_moderator_id
from backend.database import get_db
from backend.modules.tickets.schemas import ApproveRequest, BlockRequest, TicketResponse
from backend.modules.tickets.service import TicketService
from backend.modules.tickets.service_block import SoftBlockService

router = APIRouter(prefix="/api/v1", tags=["Tickets"])


@router.post(
    "/tickets/{ticket_id}/approve",
    response_model=TicketResponse,
    summary="Одобрить тикет (MOD-3)",
)
async def approve_ticket(
    ticket_id: UUID,
    request: ApproveRequest = None,
    moderator_id: UUID = Depends(get_current_moderator_id),
    db: AsyncSession = Depends(get_db),
):
    """
    spec moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/approve

    Transitions ticket IN_REVIEW → APPROVED and emits MODERATED event to B2B.
    Guards: ticket must exist, be IN_REVIEW, and belong to the caller.
    Requires at least one SKU in json_after snapshot.
    """
    comment = request.comment if request else None

    try:
        ticket = await TicketService.approve(
            db,
            ticket_id=ticket_id,
            moderator_id=moderator_id,
            comment=comment,
        )
    except LookupError:
        raise HTTPException(
            status_code=404,
            detail={"code": "NOT_FOUND", "message": "Ticket not found"},
        )
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail={"code": "NOT_ASSIGNED", "message": "This ticket is not assigned to you"},
        )
    except ValueError as exc:
        code = str(exc)
        if code == "NOT_IN_REVIEW":
            message = "Ticket is not in IN_REVIEW status (it may have been edited or already decided)"
        else:  # NO_SKU
            message = "Product has no SKUs and cannot be approved"
        raise HTTPException(
            status_code=409,
            detail={"code": code, "message": message},
        )
    except RuntimeError:
        raise HTTPException(
            status_code=502,
            detail={"code": "B2B_UNAVAILABLE", "message": "Failed to deliver MODERATED event to B2B"},
        )

    return ticket


@router.post(
    "/tickets/{ticket_id}/block",
    response_model=TicketResponse,
    summary="Мягкая блокировка тикета (MOD-4)",
)
async def block_ticket(
    ticket_id: UUID,
    request: BlockRequest,
    moderator_id: UUID = Depends(get_current_moderator_id),
    db: AsyncSession = Depends(get_db),
):
    """
    spec moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/block

    Soft-block: IN_REVIEW → BLOCKED. Emits BLOCKED + hard_block=false event to B2B.
    All blocking_reason_ids must exist and have hard_block=False.
    """
    field_reports_dicts = [fr.model_dump() for fr in request.field_reports]

    try:
        ticket = await SoftBlockService.soft_block(
            db,
            ticket_id=ticket_id,
            moderator_id=moderator_id,
            blocking_reason_ids=request.blocking_reason_ids,
            comment=request.comment,
            field_reports=field_reports_dicts,
        )
    except LookupError as exc:
        code = str(exc)
        if code == "NOT_FOUND":
            raise HTTPException(
                status_code=404,
                detail={"code": "NOT_FOUND", "message": "Ticket not found"},
            )
        # UNKNOWN_REASON
        raise HTTPException(
            status_code=400,
            detail={"code": "UNKNOWN_REASON", "message": "One or more blocking_reason_ids not found or inactive"},
        )
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail={"code": "NOT_ASSIGNED", "message": "This ticket is not assigned to you"},
        )
    except ValueError as exc:
        code = str(exc)
        if code == "HARD_BLOCK_REASON":
            raise HTTPException(
                status_code=400,
                detail={"code": "HARD_BLOCK_REASON", "message": "Use hard-block flow for reasons with hard_block=True"},
            )
        raise HTTPException(
            status_code=409,
            detail={"code": code, "message": "Ticket is not in IN_REVIEW status"},
        )
    except RuntimeError:
        raise HTTPException(
            status_code=502,
            detail={"code": "B2B_UNAVAILABLE", "message": "Failed to deliver BLOCKED event to B2B"},
        )

    return ticket
