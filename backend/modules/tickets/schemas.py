"""
Ticket decision schemas — spec moderation/openapi.yaml.

POST /api/v1/tickets/{ticket_id}/approve → TicketResponse (200)
POST /api/v1/tickets/{ticket_id}/block   → TicketResponse (200)
"""
from __future__ import annotations

from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from backend.modules.queue.schemas import TicketResponse  # noqa: F401 — re-exported


class ApproveRequest(BaseModel):
    """Optional body for POST /api/v1/tickets/{ticket_id}/approve."""
    comment: Optional[str] = Field(None, max_length=2000)


class FieldReportInput(BaseModel):
    """spec moderation/openapi.yaml#FieldReport"""
    field_path: str = Field(..., min_length=1, max_length=500)
    message: str = Field(..., min_length=1, max_length=1000)
    severity: str = Field("ERROR")

    @field_validator("severity")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in ("INFO", "WARNING", "ERROR"):
            raise ValueError("severity must be one of: INFO, WARNING, ERROR")
        return v


class BlockRequest(BaseModel):
    """spec moderation/openapi.yaml#BlockDecisionRequest"""
    blocking_reason_ids: List[UUID] = Field(..., min_length=1)
    comment: Optional[str] = Field(None, max_length=2000)
    field_reports: List[FieldReportInput] = Field(default_factory=list)
