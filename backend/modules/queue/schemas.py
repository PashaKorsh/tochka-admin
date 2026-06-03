"""
Queue schemas — spec moderation/openapi.yaml.

POST /api/v1/queue/claim → TicketResponse (200) | 204 (empty)
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ClaimRequest(BaseModel):
    """Optional body for POST /api/v1/queue/claim."""
    queue_priority: Optional[int] = Field(None, ge=1, le=4)


class TicketResponse(BaseModel):
    """
    spec moderation/openapi.yaml#TicketResponse
    required: [id, product_id, seller_id, kind, status, queue_priority, created_at]
    """
    id: UUID
    product_id: UUID
    seller_id: UUID
    kind: str                          # CREATE | EDIT
    status: str                        # IN_REVIEW after claim
    queue_priority: int
    assigned_moderator_id: Optional[UUID] = None
    claimed_at: Optional[datetime] = None
    claim_expires_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
