from fastapi import FastAPI

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
