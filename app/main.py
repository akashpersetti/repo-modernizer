from fastapi import FastAPI, Response

from app.api.routes_health import router as health_router
from app.api.routes_tasks import configure, router as tasks_router
from app.config import Settings

app = FastAPI(title="RepoModernizer")
app.include_router(health_router)
app.include_router(tasks_router)


@app.on_event("startup")
def _startup() -> None:
    settings = Settings()
    configure(settings)


@app.options("/{full_path:path}")
def cors_preflight(full_path: str) -> Response:
    # API Gateway's cors_configuration injects Access-Control-* headers onto every
    # response it forwards, but the API's single $default route sends OPTIONS to
    # this Lambda instead of letting API Gateway auto-answer it -- so the app just
    # needs to not 405 on OPTIONS; API Gateway supplies the CORS headers either way.
    return Response(status_code=200)
