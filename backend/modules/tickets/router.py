"""
Tickets router — MOD-3 approve-product flow.

Spec: moderation/openapi.yaml POST /api/v1/tickets/{ticket_id}/approve
  Auth:   Bearer JWT (moderator)
  Body:   ApproveRequest { comment? }
  200:    TicketResponse (status=APPROVED)
  403:    NOT_ASSIGNED — not this moderator's ticket
  404:    ticket not found
  409:    NOT_IN_REVIEW | NO_SKU
  502:    B2B unavailable (rare — only if B2B rejects the MODERATED event)
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth import get_current_moderator_id
from backend.database import get_db
from backend.modules.tickets.schemas import ApproveRequest, TicketResponse
from backend.modules.tickets.service import TicketService

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
