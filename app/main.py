import logging
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routes import auth, generate, history, payments, templates
from .settings import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title=settings.app_name)

# CORS configuration - как в "ии анализы 3.0" и "магазин"
cors_origins = []
if settings.cors_origins and settings.cors_origins != "*":
    cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
elif settings.frontend_url:
    cors_origins = [settings.frontend_url]

# Всегда добавляем Telegram WebApp origin
cors_origins.append('https://web.telegram.org')

# Если origins пуст, разрешаем все (для разработки)
final_origins = cors_origins if cors_origins else ['*']

app.add_middleware(
    CORSMiddleware,
    allow_origins=final_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(auth.router)
app.include_router(templates.router)
app.include_router(generate.router)
app.include_router(history.router)
app.include_router(payments.router)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Обработчик ошибок валидации - логируем детали"""
    logger.error(f"Validation error on {request.url.path}: {exc.errors()}")
    logger.error(f"Request body: {await request.body()}")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors(), "body": str(await request.body())},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}

