# main.py
import os
import logging
from typing import Optional, Dict, Any
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.memory import MemoryJobStore

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pix-pending")

# -------------------- Env --------------------
ZAPI_INSTANCE = os.getenv("ZAPI_INSTANCE", "").strip()
ZAPI_TOKEN = os.getenv("ZAPI_TOKEN", "").strip()
ZAPI_CLIENT_TOKEN = os.getenv("ZAPI_CLIENT_TOKEN", "").strip()  # if your Z-API needs it
WHATSAPP_SENDER_NAME = os.getenv("WHATSAPP_SENDER_NAME", "paginatto").strip()
MSG_TEMPLATE = os.getenv(
    "MSG_TEMPLATE",
    "Oi {name}, aqui é {brand}. Vi que você gerou um PIX do produto {product} no valor de {price} e não finalizou. "
    "Se precisar, posso te ajudar. Seu link: {checkout_url}"
).strip()

ZAPI_BASE_URL = os.getenv("ZAPI_BASE_URL", "").strip()
# If empty, code will try two common Z-API URL patterns.

# -------------------- FastAPI --------------------
app = FastAPI(title="CartPanda PIX Pending → WhatsApp", version="1.0.0")

# -------------------- APScheduler --------------------
jobstores = {"default": MemoryJobStore()}
executors = {"default": ThreadPoolExecutor(10)}
scheduler = BackgroundScheduler(jobstores=jobstores, executors=executors, timezone="UTC")
scheduler.start()

# Keep a minimal in-memory map for quick dedup or payload storage if needed.
# On ephemeral hosts this is fine for MVP. For production, move to Redis/DB.
PENDING_ORDERS: Dict[str, Dict[str, Any]] = {}

# -------------------- Models --------------------
class CartPandaCustomer(BaseModel):
    name: Optional[str] = None
    phone: Optional[str] = None  # E.164 preferred
    email: Optional[str] = None

class CartPandaProduct(BaseModel):
    title: Optional[str] = None
    price: Optional[float] = None
    checkout_url: Optional[str] = Field(default=None, description="Direct checkout URL if provided")

class CartPandaPayload(BaseModel):
    order_id: str
    payment_status: str  # "pending" | "paid" | others
    gateway: Optional[str] = None  # e.g., "pix"
    customer: Optional[CartPandaCustomer] = None
    product: Optional[CartPandaProduct] = None

# -------------------- Helpers --------------------
def _pick_zapi_send_url() -> str:
    """
    Return a Z-API send-text endpoint.
    If ZAPI_BASE_URL is set, use it as the full URL.
    Otherwise try common patterns.
    """
    if ZAPI_BASE_URL:
        return ZAPI_BASE_URL

    # Common Z-API Cloud pattern (adjust if your panel shows a different path)
    # Example documented pattern:
    candidates = [
        f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text",
        f"https://{ZAPI_INSTANCE}.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text",
    ]
    return candidates[0]

async def send_whatsapp_text(to_phone: str, text: str) -> httpx.Response:
    url = _pick_zapi_send_url()
    payload = {
        "phone": to_phone,
        "message": text,
    }
    headers = {
        "Content-Type": "application/json",
        # Some Z-API variants require a client token header. If yours does, uncomment:
        # "Client-Token": ZAPI_CLIENT_TOKEN,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(url, json=payload, headers=headers)
        return resp

def render_message(customer: CartPandaCustomer, product: CartPandaProduct) -> str:
    name = (customer.name or "").strip() or "cliente"
    brand = WHATSAPP_SENDER_NAME or "sua loja"
    price = f"R$ {product.price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") if product and product.price else "o valor informado"
    product_name = (product.title or "seu produto").strip()
    checkout_url: order.get("checkout_link") or order.get("checkout_url"),


    return MSG_TEMPLATE.format(
        name=name,
        brand=brand,
        price=price,
        product=product_name,
        checkout_url=checkout_url
    )

def schedule_whatsapp_job(order_id: str, payload: CartPandaPayload, minutes: int = 5) -> None:
    # Store payload for potential cancellation logic or debugging
    PENDING_ORDERS[order_id] = payload.dict()
    run_at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    job_id = f"pix_pending_{order_id}"

    # Remove any previous job with same id to keep idempotency
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass

    scheduler.add_job(
        func=execute_send_whatsapp_job,
        trigger="date",
        run_date=run_at,
        id=job_id,
        kwargs={"order_id": order_id},
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
    )
    log.info(f"[schedule] job={job_id} at={run_at.isoformat()}")

def cancel_whatsapp_job(order_id: str) -> bool:
    job_id = f"pix_pending_{order_id}"
    try:
        scheduler.remove_job(job_id)
        PENDING_ORDERS.pop(order_id, None)
        log.info(f"[cancel] job={job_id} canceled")
        return True
    except Exception as e:
        log.info(f"[cancel] job={job_id} not found or already executed: {e}")
        return False

async def execute_send_whatsapp_job(order_id: str) -> None:
    data = PENDING_ORDERS.pop(order_id, None)
    if not data:
        log.info(f"[execute] order_id={order_id} no payload found. Possibly already canceled.")
        return

    try:
        payload = CartPandaPayload(**data)
    except Exception as e:
        log.error(f"[execute] invalid payload for order_id={order_id}: {e}")
        return

    # If payment status flipped to paid somehow, skip.
    if payload.payment_status.lower() == "paid":
        log.info(f"[execute] order_id={order_id} already paid. Skipping send.")
        return

    # Require phone to proceed
    to_phone = (payload.customer.phone or "").strip() if payload.customer else ""
    if not to_phone:
        log.info(f"[execute] order_id={order_id} no phone. Skipping send.")
        return

    text = render_message(payload.customer or CartPandaCustomer(), payload.product or CartPandaProduct())
    resp = await send_whatsapp_text(to_phone, text)

    ok = resp.status_code in (200, 201, 202)
    log.info(f"[execute] order_id={order_id} sent={ok} status={resp.status_code} body={resp.text}")

# -------------------- Routes --------------------
@app.get("/healthz")
def healthz():
    return {"ok": True, "time": datetime.now(timezone.utc).isoformat()}

@app.post("/webhook/cartpanda")
async def cartpanda_webhook(req: Request):
    """
    Accepts events from CartPanda. Minimum fields expected:
    {
      "order_id": "123",
      "payment_status": "pending" | "paid",
      "gateway": "pix",
      "customer": {"name":"...", "phone":"+55..." },
      "product": {"title":"...", "price": 9.9, "checkout_link":"..." }
    }
    """
    try:
        raw = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        payload = CartPandaPayload(**raw)
    except Exception as e:
        log.error(f"[webhook] validation error: {e}")
        raise HTTPException(status_code=422, detail=f"Invalid payload: {e}")

    order_id = payload.order_id
    status = payload.payment_status.lower()
    gateway = (payload.gateway or "").lower()

    # Only care about PIX pending/paid
    if gateway and gateway != "pix":
        return JSONResponse({"ignored": True, "reason": "not pix", "order_id": order_id})

    if status == "pending":
        schedule_whatsapp_job(order_id, payload, minutes=5)
        return JSONResponse({"scheduled": True, "order_id": order_id, "in_minutes": 5})

    if status == "paid":
        canceled = cancel_whatsapp_job(order_id)
        return JSONResponse({"canceled": canceled, "order_id": order_id})

    return JSONResponse({"ignored": True, "reason": f"status={status}", "order_id": order_id})

# -------------------- Local dev --------------------
# Run with: uvicorn main:app --host 0.0.0.0 --port 8000
