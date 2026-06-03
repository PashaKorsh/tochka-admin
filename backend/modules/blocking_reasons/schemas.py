"""
Blocking reasons schemas — spec moderation/openapi.yaml.

GET/POST/PATCH/DELETE /api/v1/blocking-reasons[/{reason_id}]
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class BlockingReasonResponse(BaseModel):
    """spec moderation/openapi.yaml#BlockingReasonResponse"""
    id: UUID
    code: str
    title: str
    description: Optional[str] = None
    hard_block: bool
    is_active: bool

    model_config = {"from_attributes": True}


class BlockingReasonCreateRequest(BaseModel):
    """spec moderation/openapi.yaml#BlockingReasonCreateRequest"""
    code: str = Field(..., pattern=r"^[A-Z_]+$", max_length=64)
    title: str = Field(..., max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    hard_block: bool


class BlockingReasonUpdateRequest(BaseModel):
    """spec moderation/openapi.yaml#BlockingReasonUpdateRequest"""
    title: Optional[str] = Field(None, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    is_active: Optional[bool] = None
