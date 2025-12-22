import uuid
import httpx
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
    "base": {"label": "100 кредитов (100 руб)", "tokens": 100.0, "amount": 100.0},
    "neuro": {"label": "525 кредитов (499 руб)", "tokens": 525.0, "amount": 499.0},
    "vip": {"label": "1150 кредитов (999 руб)", "tokens": 1150.0, "amount": 999.0},
    "top": {"label": "2400 кредитов (1999 руб)", "tokens": 2400.0, "amount": 1999.0},
    "premium": {"label": "6500 кредитов (4999 руб)", "tokens": 6500.0, "amount": 4999.0},
}


def make_receipt(uid: int, tokens: float, amount_rub: float) -> dict:
    customer_email = f"user{uid}@ai-trends.app"
    return {
        "customer": {"email": customer_email},
        "items": [
            {
                "description": f"Пополнение {tokens:.1f} кредитов",
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
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        body = await request.json()
        logger.info(f"Payment creation request: plan_code={body.get('plan_code')}, user={user.tgid}")
    except Exception as e:
        logger.error(f"Failed to parse JSON body: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    
    plan_code = body.get("plan_code")
    if not plan_code:
        logger.error("Missing plan_code in request")
        raise HTTPException(status_code=400, detail="plan_code is required")
    
    if not _YOO_IMPORT_OK:
        logger.error("YooKassa module not available")
        raise HTTPException(status_code=503, detail="Payment gateway not configured")
    
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        logger.error("YooKassa credentials not configured")
        raise HTTPException(status_code=503, detail="Payment gateway not configured")
    
    try:
        Configuration.configure(settings.yookassa_shop_id, settings.yookassa_secret_key)
    except Exception as e:
        logger.error(f"Failed to configure YooKassa: {e}")
        raise HTTPException(status_code=503, detail=f"Payment gateway configuration error: {str(e)}")
    
    plan = BALANCE_PLANS.get(plan_code)
    if not plan:
        logger.error(f"Plan not found: {plan_code}")
        raise HTTPException(status_code=404, detail="Plan not found")
    
    amount_rub = float(plan["amount"])
    tokens = float(plan["tokens"])
    uid = user.tgid
    idem_key = str(uuid.uuid4())
    
    yoo_body = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": f"{settings.frontend_url}/profile",
        },
        "description": f"AI Trends: {tokens:g} кредитов ({plan_code})",
        "metadata": {
            "bot_user_id": str(uid),
            "plan": f"user:{plan_code}",
            "tokens": str(tokens),
        },
        "receipt": make_receipt(uid, tokens, amount_rub),
    }
    
    try:
        logger.info(f"Creating YooKassa payment: amount={amount_rub}, tokens={tokens}, idem_key={idem_key}")
        yoo_payment = YooPayment.create(yoo_body, idem_key)
        logger.info(f"YooKassa payment created: {yoo_payment.id}")
    except ApiError as exc:
        error_msg = yk_error_text(exc)
        logger.error(f"YooKassa ApiError: {error_msg}")
        raise HTTPException(status_code=400, detail=f"Payment creation failed: {error_msg}")
    except Exception as exc:
        error_msg = yk_error_text(exc)
        logger.error(f"YooKassa Exception: {type(exc).__name__}: {error_msg}")
        raise HTTPException(status_code=400, detail=f"Payment creation failed: {error_msg}")
    
    # Сохраняем платеж в БД
    try:
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
        logger.info(f"Payment saved to DB: {payment.id}")
    except Exception as e:
        logger.error(f"Failed to save payment to DB: {e}")
        await session.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save payment: {str(e)}")
    
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
    import logging
    logger = logging.getLogger(__name__)
    
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
        
        # Зачисляем кредиты пользователю
        result = await session.execute(select(User).where(User.tgid == payment.tgid))
        db_user = result.scalars().first()
        if db_user:
            db_user.balance = float(db_user.balance) + float(payment.tokens)
            
            # Отправляем вебхук о пополнении (всегда, даже без реферала)
            referral_bonus = 0.0
            referrer_tgid = None
            
            # Если пользователь был приглашен рефералом, начисляем 10% админу
            if db_user.referred_by:
                referral_bonus = float(payment.tokens) * 0.1  # 10% от пополнения
                result = await session.execute(select(User).where(User.tgid == db_user.referred_by))
                referrer = result.scalars().first()
                if referrer:
                    referrer.balance = float(referrer.balance) + referral_bonus
                    referrer_tgid = referrer.tgid
                    
            # Отправляем вебхук о пополнении (всегда)
            if settings.ref_webhook_url:
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        await client.post(
                            settings.ref_webhook_url,
                            json={
                                "referrer_tgid": referrer_tgid,
                                "referral_tgid": db_user.tgid,
                                "payment_amount": float(payment.amount),
                                "payment_tokens": float(payment.tokens),
                                "referral_bonus": referral_bonus,
                                "payment_id": str(payment.id),
                            }
                        )
                except Exception as e:
                    logger.error(f"Failed to send referral webhook: {e}")
        
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
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        payment_uuid = uuid.UUID(payment_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid payment_id format")
    
    result = await session.execute(
        select(Payment).where(Payment.id == payment_uuid, Payment.tgid == user.tgid)
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
                # Зачисляем кредиты
                result = await session.execute(select(User).where(User.tgid == payment.tgid))
                db_user = result.scalars().first()
                if db_user:
                    db_user.balance = float(db_user.balance) + float(payment.tokens)
                    
                    # Отправляем вебхук о пополнении (всегда, даже без реферала)
                    referral_bonus = 0.0
                    referrer_tgid = None
                    
                    # Если пользователь был приглашен рефералом, начисляем 10% админу
                    if db_user.referred_by:
                        referral_bonus = float(payment.tokens) * 0.1  # 10% от пополнения
                        result = await session.execute(select(User).where(User.tgid == db_user.referred_by))
                        referrer = result.scalars().first()
                        if referrer:
                            referrer.balance = float(referrer.balance) + referral_bonus
                            referrer_tgid = referrer.tgid
                            
                    # Отправляем вебхук о пополнении (всегда)
                    if settings.ref_webhook_url:
                        try:
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                await client.post(
                                    settings.ref_webhook_url,
                                    json={
                                        "referrer_tgid": referrer_tgid,
                                        "referral_tgid": db_user.tgid,
                                        "payment_amount": float(payment.amount),
                                        "payment_tokens": float(payment.tokens),
                                        "referral_bonus": referral_bonus,
                                        "payment_id": str(payment.id),
                                    }
                                )
                        except Exception as e:
                            logger.error(f"Failed to send referral webhook: {e}")
                
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

