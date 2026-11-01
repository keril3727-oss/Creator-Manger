import hashlib
import hmac
import json
import os
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

def verify_init_data(init_data: str) -> int:
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
        return int(user["id"])
    except Exception:
        raise HTTPException(401, "Не удалось определить Telegram-пользователя")

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
        # Do not expose keys or internal request headers.
        raise HTTPException(502, f"Ошибка базы данных ({response.status_code})")
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://keril3727-oss.github.io"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Telegram-Init-Data"],
)

def user_id(x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    return verify_init_data(x_telegram_init_data)

class Profile(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)
    niche: str = Field(min_length=1, max_length=50)
    followers: int = Field(ge=0, le=2_000_000_000)
    description: str = Field(default="", max_length=1000)
    photo_url: str | None = None

class Campaign(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    niche: str = Field(min_length=1, max_length=60)
    budget: float = Field(gt=0, le=100_000_000)
    currency: str = Field(default="EUR", max_length=8)
    image_url: str | None = Field(default=None, max_length=500)

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
async def bloggers():
    return await db_request("GET", "bloggers", params={
        "select": "telegram_id,username,display_name,niche,description,followers,photo_url",
        "order": "created_at.desc",
    })

@app.post("/api/profile")
async def save_profile(profile: Profile, telegram_id: int = __import__("fastapi").Depends(user_id)):
    payload = profile.model_dump()
    payload["telegram_id"] = telegram_id
    result = await db_request("POST", "bloggers", params={"on_conflict": "telegram_id"}, payload=payload)
    return result

@app.get("/api/campaigns")
async def campaigns():
    return await db_request("GET", "campaigns", params={
        "select": "id,advertiser_id,title,description,niche,budget,currency,image_url,status,created_at",
        "status": "eq.open", "order": "created_at.desc",
    })

@app.post("/api/campaigns")
async def create_campaign(campaign: Campaign, telegram_id: int = __import__("fastapi").Depends(user_id)):
    payload = campaign.model_dump()
    payload["advertiser_id"] = telegram_id
    return await db_request("POST", "campaigns", payload=payload)

@app.post("/api/campaigns/{campaign_id}/apply")
async def apply_campaign(campaign_id: int, request: ApplicationRequest, telegram_id: int = __import__("fastapi").Depends(user_id)):
    payload = {"campaign_id": campaign_id, "blogger_id": telegram_id, "message": request.message}
    return await db_request("POST", "campaign_applications", payload=payload)
