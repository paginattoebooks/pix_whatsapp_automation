# main.py — PIX pendente (checkout)

import os
import logging
from typing import Dict, Any, Optional
from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse
import httpx

# -------- Log --------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paginatto-pix")

# -------- Env --------
ZAPI_INSTANCE: str = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN: str = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN: str = os.getenv("ZAPI_CLIENT_TOKEN", "")
WHATSAPP_SENDER_NAME: str = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")

MSG_TEMPLATE: str = os.getenv(
    "MSG_TEMPLATE",
    "Oi {name}! Seu pedido via PIX de {product} no valor de {price} está aguardando pagamento. "
    "Pague aqui: {checkout_url}"
)

ZAPI_URL: str = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"

app = FastAPI(title="Paginatto - PIX pendente", version="1.0.0")

# -------- Helpers --------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("55"):
        return digits
    if len(digits) >= 10:
        return "55" + digits
    return None

def brl(value) -> str:
    if value is None:
        return "R$ 0,00"
    try:
        # admite string numérica, inteiro em centavos, etc.
        if isinstance(value, str):
            v = float(value.replace(",", "."))
        else:
            v = float(value)
        # heurística simples: inteiros grandes provavelmente estão em centavos
        if isinstance(value, int) and value >= 1000:
            v = v / 100.0
        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)

async def send_whatsapp(phone: str, message: str) -> Dict[str, Any]:
    headers = {"Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(ZAPI_URL, headers=headers, json=payload)
        return {"status": r.status_code, "body": r.text}

def safe_format(template: str, **kwargs) -> str:
    class _Safe(dict):
        def __missing__(self, k): return "{" + k + "}"
    return template.format_map(_Safe(**kwargs))

# -------- Parser (pedido/checkout) --------
def parse_order(payload: dict) -> dict:
    # alguns webhooks vêm como {"order": {...}}, outros como {"data": {...}} ou direto
    order = payload.get("order") or payload.get("data") or payload
    if not isinstance(order, dict):
        order = {}

    customer = order.get("customer") or {}
    items = order.get("line_items") or order.get("items") or []

    first = items[0] if items else {}

    # Produto: name > title + variant_title > title
    title = (first or {}).get("title")
    variant_title = (first or {}).get("variant_title")
    product_name = (first or {}).get("name") or (
        f"{title} {variant_title}".strip() if title and variant_title else title
    ) or "Seu produto"

    # Preço: tenta do item, senão do pedido
    raw_price = (first or {}).get("price") \
        or order.get("total_price") \
        or order.get("unformatted_total_price")

    price_fmt = brl(raw_price)

    # link de pagamento/checkout (garante os 3 nomes + nível raiz do payload)
    checkout_url = (
        order.get("checkout_url")
        or order.get("checkout_link")
        or order.get("cart_url")
        or payload.get("checkout_url")
        or payload.get("checkout_link")
        or payload.get("cart_url")
        or ""
    )

    # nome do cliente
    name = (
        customer.get("name")
        or customer.get("full_name")
        or customer.get("first_name")
        or "cliente"
    )

    return {
        "order_id": order.get("id"),
        "status": (order.get("status") or "").lower(),
        "payment_status": (order.get("payment_status") or "").lower(),
        "payment_method": (order.get("payment_method") or "").lower(),
        "checkout_url": checkout_url,
        "name": name,
        "phone": customer.get("phone"),
        "product": product_name,
        "price": price_fmt,
    }

# -------- Webhook --------
@app.post("/webhook/pixpendente")
async def pix_pendente_webhook(payload: Dict[str, Any] = Body(...)):
    log.info(f"[PIX] Webhook recebido: {payload}")

    info = parse_order(payload)
    event = (payload.get("event") or info.get("status") or "").lower()

    payment_method = info.get("payment_method", "")
    payment_status = info.get("payment_status", "")

    is_pix = payment_method.startswith("pix")
    is_pending = payment_status in {"pending", "pendente", "aguardando"}

    # Também aceita 'order.created' com status pendente
    trigger = is_pix and (is_pending or "order.created" in event)
    if not trigger:
        log.info(f"[{info.get('order_id')}] ignorado (event={event}, method={payment_method}, status={payment_status})")
        return JSONResponse({"ok": True, "action": "ignored", "order_id": info.get("order_id")})

    phone = normalize_phone(info.get("phone"))
    if not phone:
        log.warning(f"[{info.get('order_id')}] telefone inválido -> não enviou")
        return JSONResponse({"ok": False, "error": "telefone inválido", "order_id": info.get("order_id")})

    msg = safe_format(
        MSG_TEMPLATE,
        name=info.get("name", "cliente"),
        product=info.get("product", "seu produto"),
        price=info.get("price", "R$ 0,00"),
        checkout_url=info.get("checkout_url", "#"),
        brand=WHATSAPP_SENDER_NAME,
    )

    result = await send_whatsapp(phone, msg)
    log.info(f"[{info.get('order_id')}] WhatsApp -> {result}")
    return JSONResponse({"ok": True, "action": "whatsapp_sent", "order_id": info.get("order_id")})

# -------- Health --------
@app.get("/")
def root():
    return {"ok": True, "service": "pix-whatsapp-automation"}

@app.get("/health")
def health():
    return {"ok": True}

