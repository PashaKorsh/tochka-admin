"""
JWT auth for moderators.
Secret and algorithm match the CLAUDE.md convention; token sub = moderator_id.
"""
import os
from uuid import UUID

from fastapi import HTTPException, Header
from jose import JWTError, jwt

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-key")
JWT_ALGORITHM = "HS256"


def create_moderator_token(moderator_id: UUID) -> str:
    return jwt.encode(
        {"sub": str(moderator_id), "role": "MODERATOR"},
        JWT_SECRET_KEY,
        algorithm=JWT_ALGORITHM,
    )


async def get_current_moderator_id(
    authorization: str = Header(None),
) -> UUID:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Missing Authorization header"},
        )

    try:
        # Format: "Bearer <token>"
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise ValueError("Invalid auth header format")

        token = parts[1]
        payload = jwt.decode(
            token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM]
        )
        sub = payload.get("sub")
        if not sub:
            raise ValueError
        return UUID(str(sub))
    except (JWTError, ValueError, AttributeError, IndexError):
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHORIZED", "message": "Invalid or missing JWT"},
        )
