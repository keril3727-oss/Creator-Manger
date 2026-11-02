import hashlib
import hmac
import json
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from bot import build_application

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CRYPTOPAY_TOKEN = os.getenv("CRYPTOPAY_TOKEN") or os.getenv("CRYPTO_PAY_API_TOKEN", "")

def verify_init_data_full(init_data: str) -> dict:
    if not BOT_TOKEN or not init_data:
        raise HTTPException(401, "Открой приложение через Telegram")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Некорректные данные Telegram")
    auth_date = int(values.get("auth_date", "0") or 0)
    if abs(time.time() - auth_date) > 86400:
        raise HTTPException(401, "Сессия Telegram устарела. Перезапусти приложение")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise HTTPException(401, "Не удалось проверить пользователя Telegram")
    try:
        user = json.loads(values["user"])
        user["id"] = int(user["id"])
        return user
    except Exception:
        raise HTTPException(401, "Не удалось определить Telegram-пользователя")

def verify_init_data(init_data: str) -> int:
    return int(verify_init_data_full(init_data)["id"])

def is_business_admin(init_data: str) -> bool:
    user = verify_init_data_full(init_data)
    return str(user.get("username", "")).casefold() == "verpot"

async def db_request(method: str, table: str, *, params=None, payload=None, prefer="return=representation"):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, "Supabase пока не настроен на сервере")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": prefer,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.request(
            method, f"{SUPABASE_URL}/rest/v1/{table}",
            headers=headers, params=params, json=payload
        )
    if response.is_error:
        # Return a safe, useful database message without exposing request headers or credentials.
        try:
            error_data = response.json()
            if isinstance(error_data, dict):
                message = error_data.get("message") or error_data.get("details") or error_data.get("hint") or error_data.get("code")
                if message:
                    raise HTTPException(502, f"Ошибка базы данных: {str(message)[:500]}")
        except HTTPException:
            raise
        except Exception:
            pass
        raise HTTPException(502, f"Ошибка базы данных ({response.status_code}): {response.text[:300]}")
    return response.json() if response.content else []

@asynccontextmanager
async def lifespan(app: FastAPI):
    telegram_app = build_application()
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    app.state.telegram_app = telegram_app
    yield
    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

app = FastAPI(title="Creator Manager API", lifespan=lifespan)
# GitHub Pages and Telegram Web Mini App origins; Telegram initData is still verified.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://keril3727-oss.github.io"],
    allow_origin_regex=r"^https://(web\.telegram\.org|t\.me|telegram\.org)$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)

def user_id(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    return verify_init_data(x_telegram_init_data)

class Profile(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)
    platform_type: str = Field(default="author", pattern="^(author|channel)$")
    channel_url: str | None = Field(default=None, max_length=200)
    niche: str = Field(min_length=1, max_length=50)
    followers: int = Field(ge=0, le=2_000_000_000)
    reach: int = Field(ge=0, le=2_000_000_000)
    ad_formats: str = Field(min_length=1, max_length=200)
    price: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    photo_url: str | None = None
    username: str = Field(min_length=5, max_length=32, pattern=r"^[A-Za-z0-9_]+$")

class Campaign(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    niche: str = Field(min_length=1, max_length=300)
    budget: float = Field(gt=0, le=100_000_000)
    currency: str = Field(default="EUR", max_length=8)
    image_url: str | None = Field(default=None, max_length=500)
    contact_username: str = Field(min_length=5, max_length=32, pattern=r"^[A-Za-z0-9_]+$")

class ApplicationRequest(BaseModel):
    message: str = Field(default="", max_length=1000)

@app.post("/api/upload-image")
async def upload_image(request: Request, telegram_id: int = __import__("fastapi").Depends(user_id)):
    content_type = request.headers.get("content-type", "").split(";")[0].lower()
    allowed = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
    if content_type not in allowed:
        raise HTTPException(400, "Загрузи JPG, PNG или WEBP")
    body = await request.body()
    if not body or len(body) > 5 * 1024 * 1024:
        raise HTTPException(400, "Размер изображения должен быть до 5 МБ")
    filename = f"{telegram_id}/{int(time.time())}.{allowed[content_type]}"
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(503, "Supabase пока не настроен на сервере")
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{SUPABASE_URL}/storage/v1/object/campaign-images/{filename}",
            headers=headers, content=body
        )
    if response.is_error:
        raise HTTPException(502, "Не удалось загрузить фото. Проверь хранилище campaign-images в Supabase")
    return {"image_url": f"{SUPABASE_URL}/storage/v1/object/public/campaign-images/{filename}"}

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/api/bloggers")
async def bloggers(kind: str = "author"):
    if kind not in ("author", "channel"):
        raise HTTPException(400, "Неизвестный тип площадки")
    return await db_request("GET", "bloggers", params={
        "select": "telegram_id,username,display_name,platform_type,channel_url,niche,description,followers,reach,ad_formats,price,photo_url",
        "platform_type": f"eq.{kind}",
        "order": "created_at.desc",
    })

@app.delete("/api/admin/bloggers")
async def clear_test_bloggers(init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    # Global catalog cleanup is restricted to the verified business administrator.
    if not is_business_admin(init_data):
        raise HTTPException(403, "Недостаточно прав")
    return await db_request("DELETE", "bloggers", params={
        "telegram_id": "not.is.null",
    }, prefer="return=minimal")


@app.get("/api/profile/me")
async def get_my_profile(telegram_id: int = __import__("fastapi").Depends(user_id)):
    rows = await db_request("GET", "bloggers", params={
        "select": "telegram_id,username,display_name,platform_type,channel_url,niche,description,followers,reach,ad_formats,price,photo_url",
        "telegram_id": f"eq.{telegram_id}", "limit": "1",
    })
    return rows[0] if rows else {}


@app.post("/api/profile")
async def save_profile(profile: Profile, telegram_id: int = __import__("fastapi").Depends(user_id), init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    # Blogger profile price cap: Free up to 70 EUR; Pro and Business have no cap.
    admin_business = is_business_admin(init_data)
    sub_rows = await db_request("GET", "user_subscriptions", params={
        "select": "expires_at,plan", "user_id": f"eq.{telegram_id}", "limit": "1",
    })
    now_dt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sub_active = bool(sub_rows and sub_rows[0].get("expires_at") and sub_rows[0]["expires_at"] > now_dt)
    plan = "business" if admin_business else (sub_rows[0].get("plan", "pro").lower() if sub_active and sub_rows else "free")
    if plan == "free":
        amounts = re.findall(r"\d+(?:[.,]\d+)?", profile.price.replace(" ", ""))
        if not amounts:
            raise HTTPException(403, "На Free укажи цену числом в евро. Максимум — 70 €.")
        highest_price = max(float(value.replace(",", ".")) for value in amounts)
        if highest_price > 70:
            raise HTTPException(403, "На Free цена рекламы в профиле — максимум 70 €. Подключи Pro или Business, чтобы указать больше.")
    payload = profile.model_dump()
    telegram_user = verify_init_data_full(init_data)
    telegram_username = str(telegram_user.get("username") or "").strip()
    if not telegram_username:
        raise HTTPException(400, "Добавь username в настройках Telegram, чтобы создать профиль")
    payload["username"] = telegram_username
    payload["telegram_id"] = telegram_id
    existing = await db_request("GET", "bloggers", params={
        "select": "telegram_id", "telegram_id": f"eq.{telegram_id}", "limit": "1",
    })
    if existing:
        return await db_request("PATCH", "bloggers", params={
            "telegram_id": f"eq.{telegram_id}",
        }, payload=payload)
    return await db_request("POST", "bloggers", payload=payload)

@app.get("/api/campaigns")
async def campaigns():
    return await db_request("GET", "campaigns", params={
        "select": "id,advertiser_id,contact_username,title,description,niche,budget,currency,image_url,status,created_at",
        "status": "eq.open", "order": "created_at.desc",
    })

@app.get("/api/campaigns/mine")
async def my_campaigns(telegram_id: int = __import__("fastapi").Depends(user_id)):
    return await db_request("GET", "campaigns", params={
        "select": "id,advertiser_id,contact_username,title,description,niche,budget,currency,image_url,status,created_at",
        "advertiser_id": f"eq.{telegram_id}", "order": "created_at.desc",
    })

@app.post("/api/campaigns")
async def create_campaign(campaign: Campaign, telegram_id: int = __import__("fastapi").Depends(user_id), init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    admin_business = is_business_admin(init_data)
    sub_rows = await db_request("GET", "user_subscriptions", params={"select": "expires_at,plan", "user_id": f"eq.{telegram_id}", "limit": "1"})
    now_dt = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sub_active = bool(sub_rows and sub_rows[0].get("expires_at") and sub_rows[0]["expires_at"] > now_dt)
    plan = sub_rows[0].get("plan", "pro") if sub_active and sub_rows else "free"

    # Monthly advertiser publishing quotas: Free 0, Pro 2, Business 50.
    monthly_limits = {"free": 0, "pro": 2, "business": 50}
    limit = 50 if admin_business else monthly_limits.get(plan, 0)
    month_start = time.strftime("%Y-%m-01T00:00:00Z", time.gmtime())
    month_campaigns = await db_request("GET", "campaigns", params={
        "select": "id", "advertiser_id": f"eq.{telegram_id}",
        "created_at": f"gte.{month_start}",
    })
    if len(month_campaigns) >= limit:
        if limit == 0:
            raise HTTPException(403, "Для публикации объявления подключи Pro (до 2 в месяц) или Business (до 50 в месяц).")
        raise HTTPException(403, f"Месячный лимит объявлений исчерпан: {limit}. Перейди на Business для 50 объявлений в месяц." if plan == "pro" else "Лимит Business — 50 объявлений в месяц.")

    max_budget = float("inf") if admin_business or (sub_active and plan == "business") else (400 if sub_active and plan == "pro" else 70)
    if campaign.currency == "EUR" and campaign.budget > max_budget:
        budget_text = "без ограничений для Business" if admin_business or (sub_active and plan == "business") else ("400 € для PRO" if sub_active and plan == "pro" else "70 € для Free")
        raise HTTPException(403, "Лимит бюджета кампании: " + budget_text)
    payload = campaign.model_dump()
    payload["contact_username"] = payload["contact_username"].lstrip("@")
    payload["advertiser_id"] = telegram_id
    return await db_request("POST", "campaigns", payload=payload)

@app.patch("/api/campaigns/{campaign_id}")
async def update_campaign(campaign_id: int, campaign: Campaign, telegram_id: int = __import__("fastapi").Depends(user_id)):
    owned = await db_request("GET", "campaigns", params={
        "select": "id", "id": f"eq.{campaign_id}",
        "advertiser_id": f"eq.{telegram_id}", "limit": "1",
    })
    if not owned:
        raise HTTPException(404, "Объявление не найдено или принадлежит другому аккаунту")
    payload = campaign.model_dump()
    payload["contact_username"] = payload["contact_username"].lstrip("@")
    return await db_request("PATCH", "campaigns", params={
        "id": f"eq.{campaign_id}", "advertiser_id": f"eq.{telegram_id}",
    }, payload=payload)

@app.delete("/api/campaigns/{campaign_id}")
async def delete_campaign(campaign_id: int, telegram_id: int = __import__("fastapi").Depends(user_id)):
    owned = await db_request("GET", "campaigns", params={
        "select": "id", "id": f"eq.{campaign_id}",
        "advertiser_id": f"eq.{telegram_id}", "limit": "1",
    })
    if not owned:
        raise HTTPException(404, "Объявление не найдено или принадлежит другому аккаунту")
    await db_request("DELETE", "campaigns", params={
        "id": f"eq.{campaign_id}", "advertiser_id": f"eq.{telegram_id}",
    }, prefer="return=minimal")
    return {"ok": True}

@app.post("/api/campaigns/{campaign_id}/apply")
async def apply_campaign(campaign_id: int, request: ApplicationRequest, telegram_id: int = __import__("fastapi").Depends(user_id)):
    payload = {"campaign_id": campaign_id, "blogger_id": telegram_id, "message": request.message}
    return await db_request("POST", "campaign_applications", payload=payload)


class ProInvoiceRequest(BaseModel):
    asset: str = Field(default="USDT", pattern="^(USDT|TON)$")

@app.post("/api/pro/create-invoice")
async def create_pro_invoice(request: ProInvoiceRequest, telegram_id: int = __import__("fastapi").Depends(user_id)):
    if not CRYPTOPAY_TOKEN:
        raise HTTPException(503, "Crypto Pay не настроен на сервере")
    payload = {"currency_type": "fiat", "fiat": "USD", "amount": "15",
               "accepted_assets": request.asset, "description": "Creator Manager PRO — 30 дней",
               "payload": json.dumps({"user_id": telegram_id, "plan": "pro"}),
               "allow_comments": False, "allow_anonymous": False, "expires_in": 1800}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN}, json=payload)
    data = response.json()
    if response.is_error or not data.get("ok"):
        raise HTTPException(502, "Crypto Pay не смог создать счёт")
    invoice = data.get("result", {})
    return {"invoice_id": str(invoice.get("invoice_id")),
            "pay_url": invoice.get("bot_invoice_url") or invoice.get("web_app_invoice_url") or invoice.get("pay_url")}

@app.post("/api/business/create-invoice")
async def create_business_invoice(request: ProInvoiceRequest, telegram_id: int = __import__("fastapi").Depends(user_id)):
    if not CRYPTOPAY_TOKEN:
        raise HTTPException(503, "Crypto Pay не настроен на сервере")
    payload = {"currency_type": "fiat", "fiat": "EUR", "amount": "100",
               "accepted_assets": request.asset, "description": "Creator Manager Business — 30 дней",
               "payload": json.dumps({"user_id": telegram_id, "plan": "business"}),
               "allow_comments": False, "allow_anonymous": False, "expires_in": 1800}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post("https://pay.crypt.bot/api/createInvoice",
            headers={"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN}, json=payload)
    data = response.json()
    if response.is_error or not data.get("ok"):
        raise HTTPException(502, "Crypto Pay не смог создать счёт Business")
    invoice = data.get("result", {})
    return {"invoice_id": str(invoice.get("invoice_id")),
            "pay_url": invoice.get("bot_invoice_url") or invoice.get("web_app_invoice_url") or invoice.get("pay_url")}

@app.get("/api/pro/status")
async def pro_status(telegram_id: int = __import__("fastapi").Depends(user_id), init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    if is_business_admin(init_data):
        return {"active": True, "plan": "business", "admin": True, "expires_at": None}
    rows = await db_request("GET", "user_subscriptions", params={"select": "expires_at", "user_id": f"eq.{telegram_id}", "limit": "1"})
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    active = bool(rows and rows[0].get("expires_at") and rows[0]["expires_at"] > now)
    plan = rows[0].get("plan", "pro") if active and rows else "free"
    return {"active": active, "plan": plan, "admin": False, "expires_at": rows[0].get("expires_at") if rows else None}

async def activate_subscription(invoice_id: str, telegram_id: int, expected_plan: str):
    if not CRYPTOPAY_TOKEN:
        raise HTTPException(503, "Crypto Pay не настроен на сервере")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get("https://pay.crypt.bot/api/getInvoices",
            headers={"Crypto-Pay-API-Token": CRYPTOPAY_TOKEN},
            params={"invoice_ids": invoice_id, "count": 1})
    data = response.json()
    if response.is_error or not data.get("ok"):
        raise HTTPException(502, "Не удалось проверить оплату")
    result = data.get("result", [])
    invoices = result if isinstance(result, list) else result.get("items", [])
    invoice = next((x for x in invoices if str(x.get("invoice_id")) == invoice_id), None)
    try:
        invoice_payload = json.loads(invoice.get("payload", "{}")) if invoice else {}
    except (TypeError, ValueError):
        invoice_payload = {}
    # Legacy PRO invoices used the Telegram ID directly in payload.
    valid_owner = invoice_payload.get("user_id") == telegram_id or invoice.get("payload") == str(telegram_id) if invoice else False
    valid_plan = invoice_payload.get("plan", "pro") == expected_plan if invoice else False
    if not invoice or not valid_owner or not valid_plan:
        raise HTTPException(404, "Счёт не найден")
    if invoice.get("status") != "paid":
        return {"paid": False}
    import datetime
    base = datetime.datetime.now(datetime.timezone.utc)
    rows = await db_request("GET", "user_subscriptions", params={"select": "expires_at,last_invoice_id,plan", "user_id": f"eq.{telegram_id}", "limit": "1"})
    if rows and rows[0].get("last_invoice_id") == invoice_id:
        return {"paid": True, "plan": rows[0].get("plan", expected_plan), "expires_at": rows[0].get("expires_at")}
    if rows and rows[0].get("expires_at"):
        try:
            old = datetime.datetime.fromisoformat(rows[0]["expires_at"].replace("Z", "+00:00"))
            if old > base:
                base = old
        except ValueError:
            pass
    expiry = (base + datetime.timedelta(days=30)).isoformat().replace("+00:00", "Z")
    existing_subscription = await db_request("GET", "user_subscriptions", params={
        "select": "user_id", "user_id": f"eq.{telegram_id}", "limit": "1",
    })
    subscription_payload = {"user_id": telegram_id, "expires_at": expiry, "last_invoice_id": invoice_id, "plan": expected_plan}
    if existing_subscription:
        await db_request("PATCH", "user_subscriptions", params={
            "user_id": f"eq.{telegram_id}",
        }, payload=subscription_payload)
    else:
        await db_request("POST", "user_subscriptions", payload=subscription_payload)
    return {"paid": True, "plan": expected_plan, "expires_at": expiry}

@app.post("/api/pro/verify/{invoice_id}")
async def verify_pro_invoice(invoice_id: str, telegram_id: int = __import__("fastapi").Depends(user_id)):
    return await activate_subscription(invoice_id, telegram_id, "pro")

@app.post("/api/business/verify/{invoice_id}")
async def verify_business_invoice(invoice_id: str, telegram_id: int = __import__("fastapi").Depends(user_id)):
    return await activate_subscription(invoice_id, telegram_id, "business")
