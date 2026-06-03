"""
Ticket decision schemas — spec moderation/openapi.yaml.

POST /api/v1/tickets/{ticket_id}/approve → TicketResponse (200)
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from backend.modules.queue.schemas import TicketResponse  # noqa: F401 — re-exported


class ApproveRequest(BaseModel):
    """Optional body for POST /api/v1/tickets/{ticket_id}/approve."""
    comment: Optional[str] = Field(None, max_length=2000)
