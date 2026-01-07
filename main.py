import os
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse, Response
import httpx

from apscheduler.schedulers.background import BackgroundScheduler

# ---------------- Log ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("paginatto-pix")

# ---------------- Env (UAZAPI) ----------------
# Coloque aqui A URL COMPLETA igual seu teste:
# https://paginatto.uazapi.com/send/text
UAZAPI_SEND_URL = os.getenv("UAZAPI_SEND_URL", "").strip()
UAZAPI_TOKEN = os.getenv("UAZAPI_TOKEN", "").strip()

WHATSAPP_SENDER_NAME = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")

MSG_TEMPLATE = os.getenv(
    "MSG_TEMPLATE",
    "Oi {name}! Seu pedido via PIX de {product} no valor de {price} "
    "ainda está aguardando pagamento. Se quiser concluir, é só pagar aqui: {checkout_url}\n— {brand}"
)

# ---------------- App & Scheduler ----------------
app = FastAPI(title="Paginatto - PIX pendente", version="3.0.0")

scheduler = BackgroundScheduler(timezone="UTC")

# guarda pedidos confirmados como pagos (memória volátil: reinicia em deploy)
paid_orders: set[int] = set()


# ---------------- Helpers ----------------
def normalize_phone(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("55"):
        return digits
    if len(digits) >= 10:
        return "55" + digits
    return None


def to_lower(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def brl(value) -> str:
    if value is None:
        return "R$ 0,00"
    try:
        if isinstance(value, dict):
            value = value.get("amount") or value.get("value") or value.get("price") or value

        if isinstance(value, str):
            v = float(value.replace(",", "."))
        else:
            v = float(value)

        # heurística: se vier inteiro grande, pode ser centavos
        if isinstance(value, int) and value >= 1000:
            v = v / 100.0

        return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def safe_format(template: str, **kwargs) -> str:
    return (template or "").format_map(_SafeDict(kwargs))


def uazapi_send_text_sync(phone: str, message: str) -> Dict[str, Any]:
    """
    UAZAPI conforme seu teste:
    POST {UAZAPI_SEND_URL}
    headers: token, Content-Type
    body: number, text
    """
    if not UAZAPI_SEND_URL:
        return {"ok": False, "status": "error", "error": "missing_env:UAZAPI_SEND_URL"}
    if not UAZAPI_TOKEN:
        return {"ok": False, "status": "error", "error": "missing_env:UAZAPI_TOKEN"}

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "token": UAZAPI_TOKEN,
    }
    payload = {"number": phone, "text": message}

    try:
        r = httpx.post(UAZAPI_SEND_URL, headers=headers, json=payload, timeout=30)
        try:
            body = r.json()
        except Exception:
            body = r.text
        return {"ok": r.status_code < 300, "status": r.status_code, "body": body, "url": UAZAPI_SEND_URL}
    except Exception as e:
        log.exception(f"Erro ao enviar UAZAPI: {e}")
        return {"ok": False, "status": "error", "error": str(e), "url": UAZAPI_SEND_URL}


# ---------------- Parser ----------------
def parse_order(payload: dict) -> dict:
    order = payload.get("order") or payload.get("data") or payload
    if not isinstance(order, dict):
        order = {}

    customer = order.get("customer") or {}
    items = order.get("line_items") or order.get("items") or []
    first = items[0] if items else {}

    title = (first or {}).get("title")
    variant_title = (first or {}).get("variant_title")
    product_name = (first or {}).get("name") or (
        f"{title} {variant_title}".strip() if title and variant_title else title
    ) or "Seu produto"

    raw_price = (
        (first or {}).get("price")
        or order.get("total_price_in_decimal")
        or order.get("total_price")
        or order.get("subtotal_price")
    )

    checkout_url = (
        order.get("checkout_url")
        or order.get("checkout_link")
        or order.get("cart_url")
        or payload.get("checkout_url")
        or payload.get("checkout_link")
        or payload.get("cart_url")
        or ""
    )

    status = to_lower(order.get("status") or order.get("status_id"))

    payment_status_raw = order.get("payment_status")
    payment_block = order.get("payment")
    if payment_status_raw is None and isinstance(payment_block, dict):
        payment_status_raw = payment_block.get("status_id") or payment_block.get("actual_status_id")
    payment_status = to_lower(payment_status_raw)

    method_raw = order.get("payment_method") or order.get("payment_type") or order.get("payment_gateway")
    if not method_raw and isinstance(payment_block, dict):
        method_raw = payment_block.get("type") or payment_block.get("payment_type")
    payment_method = to_lower(method_raw)

    name = (
        customer.get("name")
        or customer.get("full_name")
        or ((customer.get("first_name") or "") + (" " + customer.get("last_name") if customer.get("last_name") else ""))
        or "cliente"
    ).strip()

    phone = customer.get("phone") or order.get("phone")

    return {
        "order_id": order.get("id"),
        "status": status,
        "payment_status": payment_status,
        "payment_method": payment_method,
        "checkout_url": checkout_url,
        "name": name,
        "phone": phone,
        "product": product_name,
        "price": brl(raw_price),
    }


def is_pix_pending(info: dict, event: str) -> bool:
    payment_method = to_lower(info.get("payment_method"))
    payment_status = to_lower(info.get("payment_status"))
    order_status = to_lower(info.get("status"))

    is_pix = payment_method.startswith("pix")
    is_pending = payment_status in {"1", "0", "pending", "pendente", "aguardando"} or order_status in {
        "new", "open", "pending", "pendente", "aguardando"
    }

    # aceita gatilho em order.created / pending
    return is_pix and (("order.created" in event) or is_pending)


def is_really_paid(info: dict) -> bool:
    payment_status = to_lower(info.get("payment_status"))
    order_status = to_lower(info.get("status"))

    # Só considera pago se vier confirmação real (ajuste se você observar outro código nos logs)
    return payment_status in {"3", "paid", "pago", "aprovado", "approved"} or order_status in {"paid", "pago"}


# ---------------- Job (5 min) ----------------
def check_and_send_if_still_pending(order_id: int, name: str, phone: str, product: str, price: str, checkout_url: str):
    log.info(f"[{order_id}] job 5min iniciado")

    if order_id in paid_orders:
        log.info(f"[{order_id}] já confirmado como pago; não envia.")
        return

    phone_norm = normalize_phone(phone)
    if not phone_norm:
        log.warning(f"[{order_id}] telefone inválido; não envia.")
        return

    msg = safe_format(
        MSG_TEMPLATE,
        name=name or "cliente",
        product=product or "seu produto",
        price=price or "R$ 0,00",
        checkout_url=checkout_url or "",
        brand=WHATSAPP_SENDER_NAME,
    )

    result = uazapi_send_text_sync(phone_norm, msg)
    log.info(f"[{order_id}] WhatsApp (5min) -> {result}")


# ---------------- Webhook ----------------
@app.post("/webhook/pixpendente")
@app.post("/webhook/cartpanda")
async def pix_pendente_webhook(payload: Dict[str, Any] = Body(...)):
    info = parse_order(payload)

    event_raw = payload.get("event")
    if isinstance(event_raw, dict):
        event_raw = event_raw.get("type") or event_raw.get("name") or event_raw
    event = to_lower(event_raw or info.get("status"))

    order_id = info.get("order_id")
    if order_id is None:
        return JSONResponse({"ok": False, "error": "missing_order_id"})

    # 1) Se estiver REALMENTE pago, marca e cancela job
    if is_really_paid(info):
        paid_orders.add(int(order_id))
        try:
            scheduler.remove_job(f"pix_{order_id}")
        except Exception:
            pass
        log.info(f"[{order_id}] confirmado pago (event={event}, payment_status={info.get('payment_status')})")
        return JSONResponse({"ok": True, "action": "marked_paid", "order_id": order_id})

    # 2) Se for PIX pendente, agenda job 5min
    if is_pix_pending(info, event):
        run_at = datetime.utcnow() + timedelta(minutes=5)
        scheduler.add_job(
            check_and_send_if_still_pending,
            "date",
            run_date=run_at,
            id=f"pix_{order_id}",
            replace_existing=True,
            args=[
                int(order_id),
                info.get("name"),
                info.get("phone"),
                info.get("product"),
                info.get("price"),
                info.get("checkout_url"),
            ],
        )
        log.info(f"[{order_id}] agendado 5min em {run_at.isoformat()} UTC (event={event}, pay={info.get('payment_status')})")
        return JSONResponse({"ok": True, "action": "scheduled_5min_check", "order_id": order_id})

    # 3) Ignora o resto
    log.info(f"[{order_id}] ignorado (event={event})")
    return JSONResponse({"ok": True, "action": "ignored", "order_id": order_id, "event": event})


# ---------------- Health ----------------
@app.get("/")
def root():
    return {"ok": True, "service": "pix-pendente", "provider": "uazapi"}

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/favicon.ico")
@app.get("/favicon.png")
def favicon():
    return Response(status_code=204)


# ---------------- Startup ----------------
@app.on_event("startup")
def _start_scheduler():
    if not scheduler.running:
        scheduler.start()
        log.info("Scheduler iniciado.")
