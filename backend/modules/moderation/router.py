"""
Moderation events router — MOD-1 receive-product-events.

Spec: moderation/openapi.yaml POST /api/v1/b2b/events
  Header: X-Service-Key (required)
  Body:   IncomingB2BEvent {event_type, idempotency_key, occurred_at, payload}
  202:    Accepted (first call and idempotent replay)
  401:    Missing or invalid X-Service-Key
"""
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.modules.moderation.schemas import IncomingB2BEvent
from backend.modules.moderation.service import ModerationService

_B2B_TO_MOD_KEY = os.getenv("B2B_TO_MOD_KEY", "dev-service-key")

router = APIRouter(prefix="/api/v1", tags=["B2B Events"])


async def _require_service_key(
    x_service_key: Optional[str] = Header(None, alias="X-Service-Key"),
) -> None:
    """Validate inter-service auth header from B2B."""
    if x_service_key is None or x_service_key != _B2B_TO_MOD_KEY:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Missing or invalid X-Service-Key"},
        )


@router.post(
    "/b2b/events",
    status_code=202,
    summary="Receive product events from B2B (MOD-1)",
)
async def receive_b2b_event(
    event: IncomingB2BEvent,
    _: None = Depends(_require_service_key),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    spec moderation/openapi.yaml POST /api/v1/b2b/events

    Accepts PRODUCT_CREATED, PRODUCT_EDITED, PRODUCT_DELETED from B2B.
    Idempotent by idempotency_key — duplicate events return 202 without side effects.

    Auth: X-Service-Key header required. 401 if missing or invalid.
    """
    await ModerationService.handle_event(db, event)
    return JSONResponse(status_code=202, content={"accepted": True})
