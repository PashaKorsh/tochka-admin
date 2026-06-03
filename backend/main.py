"""
NeoMarket Moderation API — FastAPI application entry-point.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.database import create_tables
from backend.modules.moderation.router import router as moderation_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    yield


app = FastAPI(
    title="NeoMarket Moderation API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        body = {"code": detail["code"], "message": detail["message"]}
    else:
        body = {"code": "ERROR", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content=body)


app.include_router(moderation_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
