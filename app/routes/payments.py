import uuid
from fastapi import APIRouter, Depends, HTTPException
from starlette.requests import Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from .. import schemas
from ..auth import get_current_user
from ..db import get_session
from ..models import Payment, User
from ..settings import settings

try:
    from yookassa import Configuration, Payment as YooPayment
    from yookassa.domain.exceptions import ApiError
    _YOO_IMPORT_OK = True
except ModuleNotFoundError:
    Configuration = YooPayment = None

    class ApiError(Exception):
        pass

    _YOO_IMPORT_OK = False

router = APIRouter(prefix="/payments", tags=["payments"])

# Планы пополнения баланса
BALANCE_PLANS = {
    "trial": {"label": "Пробные токены: 2 шт (120 руб)", "tokens": 2.0, "amount": 120.0},
    "base": {"label": "База: 12 токенов (470 руб)", "tokens": 12.0, "amount": 470.0},
    "neuro": {"label": "Нейро: 30 токенов (900 руб)", "tokens": 30.0, "amount": 900.0},
    "vip": {"label": "Вип: 120 токенов (3400 руб)", "tokens": 120.0, "amount": 3400.0},
    "top": {"label": "Топ: 600 токенов (16000 руб)", "tokens": 600.0, "amount": 16000.0},
}


def make_receipt(uid: int, tokens: float, amount_rub: float) -> dict:
    customer_email = f"user{uid}@ai-trends.app"
    return {
        "customer": {"email": customer_email},
        "items": [
            {
                "description": f"Пополнение {tokens:.1f} токенов",
                "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
                "quantity": "1.0",
                "vat_code": 1,
                "payment_subject": "service",
                "payment_mode": "full_payment",
            }
        ],
        "tax_system_code": 1,
    }


def yk_error_text(exc: Exception) -> str:
    if isinstance(exc, ApiError):
        return str(getattr(exc, "message", exc))
    return str(exc)


@router.get("/plans")
async def get_payment_plans():
    """Получить список планов пополнения"""
    return {"plans": BALANCE_PLANS}


@router.post("/create")
async def create_payment(
    request: Request,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Создать платеж через ЮКассу"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    plan_code = body.get("plan_code")
    if not plan_code:
        raise HTTPException(status_code=400, detail="plan_code is required")
    """Создать платеж через ЮКассу"""
    if not _YOO_IMPORT_OK or not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        raise HTTPException(status_code=503, detail="Payment gateway not configured")
    
    Configuration.configure(settings.yookassa_shop_id, settings.yookassa_secret_key)
    
    plan = BALANCE_PLANS.get(plan_code)
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    
    amount_rub = float(plan["amount"])
    tokens = float(plan["tokens"])
    plan_label = plan["label"]
    uid = user.tgid
    idem_key = str(uuid.uuid4())
    
    yoo_body = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"{settings.frontend_url}/profile",
        },
        "description": f"AI Trends: {tokens:g} токенов ({plan_code})",
        "metadata": {
            "bot_user_id": str(uid),
            "plan": f"user:{plan_code}",
            "tokens": str(tokens),
        },
        "receipt": make_receipt(uid, tokens, amount_rub),
    }
    
    try:
        yoo_payment = YooPayment.create(yoo_body, idem_key)
    except ApiError as exc:
        raise HTTPException(status_code=400, detail=f"Payment creation failed: {yk_error_text(exc)}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Payment creation failed: {yk_error_text(exc)}")
    
    # Сохраняем платеж в БД
    payment = Payment(
        tgid=uid,
        yookassa_payment_id=yoo_payment.id,
        amount=amount_rub,
        tokens=tokens,
        status="pending",
        plan_code=plan_code,
    )
    session.add(payment)
    await session.commit()
    await session.refresh(payment)
    
    return {
        "payment_id": str(payment.id),
        "yookassa_payment_id": yoo_payment.id,
        "confirmation_url": yoo_payment.confirmation.confirmation_url,
        "amount": amount_rub,
        "tokens": tokens,
    }


@router.post("/webhook")
async def yookassa_webhook(request: Request, session: AsyncSession = Depends(get_session)):
    """Webhook от ЮКассы для уведомления о статусе платежа"""
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "detail": "Invalid JSON"}
    
    event = body.get("event")
    if event != "payment.succeeded":
        return {"status": "ignored"}
    
    payment_object = body.get("object", {})
    yoo_payment_id = payment_object.get("id")
    if not yoo_payment_id:
        return {"status": "error", "detail": "No payment id"}
    
    try:
        result = await session.execute(
            select(Payment).where(Payment.yookassa_payment_id == yoo_payment_id)
        )
        payment = result.scalars().first()
        if not payment:
            return {"status": "error", "detail": "Payment not found"}
        
        if payment.status == "succeeded":
            return {"status": "already_processed"}
        
        # Обновляем статус платежа
        payment.status = "succeeded"
        
        # Зачисляем токены пользователю
        result = await session.execute(select(User).where(User.tgid == payment.tgid))
        db_user = result.scalars().first()
        if db_user:
            db_user.balance = float(db_user.balance) + float(payment.tokens)
        
        await session.commit()
    except Exception as e:
        await session.rollback()
        return {"status": "error", "detail": str(e)}
    
    return {"status": "ok"}


@router.get("/status/{payment_id}")
async def get_payment_status(
    payment_id: str,
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Проверить статус платежа"""
    result = await session.execute(
        select(Payment).where(Payment.id == payment_id, Payment.tgid == user.tgid)
    )
    payment = result.scalars().first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    
    # Проверяем статус в ЮКассе, если платеж еще pending
    if payment.status == "pending" and _YOO_IMPORT_OK and payment.yookassa_payment_id:
        try:
            Configuration.configure(settings.yookassa_shop_id, settings.yookassa_secret_key)
            yoo_payment = YooPayment.find_one(payment.yookassa_payment_id)
            if yoo_payment.status == "succeeded" and payment.status != "succeeded":
                payment.status = "succeeded"
                # Зачисляем токены
                result = await session.execute(select(User).where(User.tgid == payment.tgid))
                db_user = result.scalars().first()
                if db_user:
                    db_user.balance = float(db_user.balance) + float(payment.tokens)
                await session.commit()
        except Exception:
            pass
    
    return {
        "id": str(payment.id),
        "status": payment.status,
        "amount": float(payment.amount),
        "tokens": float(payment.tokens),
        "created_at": payment.created_at.isoformat(),
    }


@router.get("/history")
async def get_payment_history(
    user=Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Получить историю платежей пользователя"""
    result = await session.execute(
        select(Payment)
        .where(Payment.tgid == user.tgid)
        .order_by(Payment.created_at.desc())
        .limit(50)
    )
    payments = result.scalars().all()
    return [
        {
            "id": str(p.id),
            "amount": float(p.amount),
            "tokens": float(p.tokens),
            "status": p.status,
            "plan_code": p.plan_code,
            "created_at": p.created_at.isoformat(),
        }
        for p in payments
    ]

