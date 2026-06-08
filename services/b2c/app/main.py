from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse

from app.database import init_db
from app.routers.catalog import router as catalog_router
from app.routers.favorites import router as favorites_router


def make_lifespan(init_database: bool):
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if init_database:
            init_db()
        yield

    return lifespan


def create_app(init_database: bool = True) -> FastAPI:
    app = FastAPI(
        title="NeoMarket B2C Service",
        lifespan=make_lifespan(init_database),
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict) and {"code", "message"} <= exc.detail.keys():
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": "ERROR", "message": str(exc.detail)},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": "INVALID_REQUEST", "message": "Invalid request"},
        )

    app.include_router(catalog_router)
    app.include_router(favorites_router)
    return app


app = create_app()
