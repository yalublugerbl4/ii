import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import auth, generate, history, payments, templates
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
app.include_router(payments.router)


@app.get("/health")
async def health():
    return {"status": "ok"}

