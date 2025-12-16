import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db
from .routes import auth, generate, history, templates
from .settings import settings

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.app_name)

origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(templates.router)
app.include_router(generate.router)
app.include_router(history.router)


@app.on_event("startup")
async def startup_event():
    await init_db()


@app.get("/health")
async def health():
    return {"status": "ok"}

