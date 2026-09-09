"""FastAPI application.

Only the operational surface is wired up so far: health, readiness and metrics.
The CDR/media API and the HTMX UI are added in later phases; keeping this module
honest means a deploy either works or fails visibly, rather than serving routes
that pretend to function.

``/api/health`` is a liveness probe and must not touch the database -- if
PostgreSQL is down, restarting the API changes nothing, so a failing liveness
check would only add a restart loop to an existing outage.  ``/api/ready``
reports dependencies, and that is what a load balancer should gate on.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from c2w import __version__
from c2w.db.session import dispose_engine, get_engine, get_sessionmaker
from c2w.logging import configure_logging, get_logger, reconfigure_from_settings
from c2w.settings import settings_service

log = get_logger(__name__)
_started_at = time.time()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging("c2w-api")
    # Log settings live in the database, so start with safe defaults and
    # re-apply once the database is reachable. A database that is down must not
    # stop the API from starting -- readiness reports it instead.
    try:
        factory = get_sessionmaker()
        async with factory() as session:
            await reconfigure_from_settings("c2w-api", session)
            environment = await settings_service.get_str(session, "core.environment")
    except Exception as exc:
        environment = "unknown"
        log.warning("api.settings_unavailable_at_startup", error=str(exc))
    log.info("api.starting", version=__version__, environment=environment)
    yield
    await dispose_engine()
    log.info("api.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="c2w",
        version=__version__,
        description="CommPeak to Wasabi recording offload and CDR platform",
        lifespan=lifespan,
        # Interactive docs stay off: the environment setting lives in the
        # database and is not readable at import time, and this API is internal
        # anyway. Enable them deliberately in development if needed.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/api/health", response_class=JSONResponse, tags=["ops"])
    async def health() -> dict[str, Any]:
        """Liveness. Deliberately dependency-free."""
        return {
            "status": "ok",
            "version": __version__,
            "uptime_seconds": round(time.time() - _started_at, 1),
        }

    @app.get("/api/ready", tags=["ops"])
    async def ready() -> Response:
        """Readiness. Reports each dependency and fails with 503 if any is down."""
        checks: dict[str, str] = {}
        ok = True
        try:
            engine = get_engine()
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            ok = False
            checks["database"] = f"error: {type(exc).__name__}"
            log.warning("readiness.database_failed", error=str(exc))

        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks},
            status_code=200 if ok else 503,
        )

    @app.get("/api/metrics", response_class=PlainTextResponse, tags=["ops"])
    async def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        factory = get_sessionmaker()
        async with factory() as session:
            if not await settings_service.get_bool(session, "observability.metrics_enabled"):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "metrics are disabled")
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # Static assets and the HTML UI. htmx and Alpine are vendored rather than
    # loaded from a CDN: this runs on a private network and must not depend on
    # outbound internet access to render a page.
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent.parent / "web" / "static")),
        name="static",
    )

    from c2w.web.routes import router as web_router

    app.include_router(web_router)

    @app.exception_handler(HTTPException)
    async def unauthorised_to_login(request: Request, exc: HTTPException) -> Response:
        """Send browsers to the sign-in page rather than showing raw JSON 401s."""
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and "text/html" in request.headers.get(
            "accept", ""
        ):
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
