"""
Moderation schemas — spec moderation/openapi.yaml.

IncomingB2BEvent (POST /api/v1/b2b/events):
  required: [event_type, idempotency_key, occurred_at, payload]
  event_type: PRODUCT_CREATED | PRODUCT_EDITED | PRODUCT_DELETED
  payload: EventProductCreated | EventProductEdited | EventProductDeleted
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class B2BEventType(str, Enum):
    PRODUCT_CREATED = "PRODUCT_CREATED"
    PRODUCT_EDITED = "PRODUCT_EDITED"
    PRODUCT_DELETED = "PRODUCT_DELETED"


class EventProductCreated(BaseModel):
    """spec moderation/openapi.yaml#EventProductCreated"""
    product_id: UUID
    seller_id: UUID
    category_id: Optional[UUID] = None
    queue_priority: int = Field(default=3, ge=1, le=4)
    json_after: Dict[str, Any] = Field(default_factory=dict)


class EventProductEdited(BaseModel):
    """spec moderation/openapi.yaml#EventProductEdited"""
    product_id: UUID
    seller_id: UUID
    category_id: Optional[UUID] = None
    queue_priority: int = Field(default=3, ge=1, le=4)
    json_before: Dict[str, Any] = Field(default_factory=dict)
    json_after: Dict[str, Any] = Field(default_factory=dict)


class EventProductDeleted(BaseModel):
    """spec moderation/openapi.yaml#EventProductDeleted"""
    product_id: UUID


class IncomingB2BEvent(BaseModel):
    """
    spec moderation/openapi.yaml#IncomingB2BEvent
    required: [event_type, idempotency_key, occurred_at, payload]
    """
    event_type: B2BEventType
    idempotency_key: UUID
    occurred_at: datetime
    payload: Dict[str, Any] = Field(default_factory=dict)
