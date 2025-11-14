# main.py — PIX pendente (controle de 5 minutos, Render)

import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse, Response
import httpx

from apscheduler.schedulers.background import BackgroundScheduler

# ---------------- Log ----------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("paginatto-pix")

# ---------------- Env ----------------
ZAPI_INSTANCE: str = os.getenv("ZAPI_INSTANCE", "")
ZAPI_TOKEN: str = os.getenv("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN: str = os.getenv("ZAPI_CLIENT_TOKEN", "")
WHATSAPP_SENDER_NAME: str = os.getenv("WHATSAPP_SENDER_NAME", "Paginatto")

MSG_TEMPLATE: str = os.getenv(
    "MSG_TEMPLATE",
    "Oi {name}! Seu pedido via PIX de {product} no valor de {price} "
    "ainda está aguardando pagamento. Se quiser concluir, é só pagar aqui: {checkout_url}"
)

ZAPI_URL: str = (
    f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}/send-text"
)

# ---------------- FastAPI & Scheduler ----------------
app = FastAPI(title="Paginatto - PIX pendente", version="2.0.0")

# Scheduler em background (funciona bem em Render / processo sempre ligado)
scheduler = BackgroundScheduler(timezone="UTC")

# guarda pedidos que já foram pagos (pra não mandar msg)
paid_orders = set()


# ---------------- Helpers ----------------
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
        # aceita string numérica, inteiro em centavos, etc.
        if isinstance(value, dict):
            # tenta pegar chaves comuns, se vier algo tipo {"amount": 2089}
            value = (
                value.get("amount")
                or value.get("value")
                or value.get("price")
                or value
            )

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


def to_lower(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip().lower()
    except Exception:
        return ""


async def send_whatsapp(phone: str, message: str) -> Dict[str, Any]:
    headers = {"Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": phone, "message": message}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(ZAPI_URL, headers=headers, json=payload)
            return {"status": r.status_code, "body": r.text}
    except Exception as e:
        log.exception(f"Erro ao enviar WhatsApp para {phone}: {e}")
        return {"status": "error", "body": str(e)}


def safe_format(template: str, **kwargs) -> str:
    class _Safe(dict):
        def __missing__(self, k):
            return "{" + k + "}"

    return template.format_map(_Safe(**kwargs))


# ---------------- Parser (pedido/checkout) ----------------
def parse_order(payload: dict) -> dict:
    """
    Normaliza o JSON do CartPanda em um dict simples com:
    - order_id
    - status (New / Paid / etc.)
    - payment_status (1, 3, "pending", etc.)
    - payment_method (pix, boleto, card...)
    - checkout_url
    - name
    - phone
    - product
    - price (formatado em BRL)
    """
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
    raw_price = (
        (first or {}).get("price")
        or order.get("total_price_in_decimal")
        or order.get("total_price")
        or order.get("subtotal_price")
    )

    price_fmt = brl(raw_price)

    # link de pagamento/checkout
    checkout_url = (
        order.get("checkout_url")
        or order.get("checkout_link")
        or order.get("cart_url")
        or payload.get("checkout_url")
        or payload.get("checkout_link")
        or payload.get("cart_url")
        or ""
    )

    # status geral do pedido (New, Paid etc.)
    status = to_lower(order.get("status") or order.get("status_id"))

    # ---- payment_status: pode vir em vários lugares ----
    payment_status_raw = order.get("payment_status")

    payment_block = order.get("payment")
    if payment_status_raw is None and isinstance(payment_block, dict):
        payment_status_raw = (
            payment_block.get("status_id")
            or payment_block.get("actual_status_id")
        )

    payment_status = to_lower(payment_status_raw)

    # ---- payment_method: CartPanda usa "payment_type" e blocos de payment ----
    method_raw = (
        order.get("payment_method")
        or order.get("payment_type")  # ex.: "pix"
        or order.get("payment_gateway")  # ex.: "mercadopago"
    )

    if not method_raw and isinstance(payment_block, dict):
        method_raw = (
            payment_block.get("type")           # ex.: "pix"
            or payment_block.get("payment_type")
        )

    all_payments = order.get("all_payments")
    if not method_raw and isinstance(all_payments, list) and all_payments:
        method_raw = all_payments[0].get("type")

    payment_method = to_lower(method_raw)

    # nome do cliente
    name = (
        customer.get("name")
        or customer.get("full_name")
        or (customer.get("first_name") or "")
        + (" " + customer.get("last_name") if customer.get("last_name") else "")
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
        "price": price_fmt,
    }


# ---------------- Job de 5 minutos ----------------
def check_and_send_if_still_pending(
    order_id: int,
    name: str,
    phone: str,
    product: str,
    price: str,
    checkout_url: str,
):
    """
    Roda 5 minutos depois do order.created (PIX).
    Se o pedido ainda não foi marcado como pago, manda WhatsApp.
    """

    log.info(f"[{order_id}] job 5min iniciado para checar PIX pendente")

    # 1) se já foi pago, não manda nada
    if order_id in paid_orders:
        log.info(f"[{order_id}] já foi pago antes dos 5 minutos; não envia mensagem.")
        return

    # 2) normaliza telefone
    phone_norm = normalize_phone(phone)
    if not phone_norm:
        log.warning(f"[{order_id}] telefone inválido na checagem; não envia.")
        return

    # 3) monta mensagem
    msg = safe_format(
        MSG_TEMPLATE,
        name=name or "cliente",
        product=product or "seu produto",
        price=price or "R$ 0,00",
        checkout_url=checkout_url or "#",
        brand=WHATSAPP_SENDER_NAME,
    )

    # 4) chama a função assíncrona de envio (scheduler roda em thread separada)
    try:
        result = asyncio.run(send_whatsapp(phone_norm, msg))
        log.info(f"[{order_id}] WhatsApp (5min) -> {result}")
    except Exception as e:
        log.exception(f"[{order_id}] erro ao enviar WhatsApp pós-5min: {e}")


# ---------------- Webhook ----------------
@app.post("/webhook/pixpendente")
@app.post("/webhook/cartpanda")
async def pix_pendente_webhook(payload: Dict[str, Any] = Body(...)):
    log.info(f"[PIX] Webhook recebido: {payload}")

    info = parse_order(payload)

    # event pode vir em vários formatos
    event_raw = payload.get("event")
    if isinstance(event_raw, dict):
        event_raw = (
            event_raw.get("type")
            or event_raw.get("name")
            or event_raw
        )
    event = to_lower(event_raw or info.get("status"))

    payment_method = to_lower(info.get("payment_method"))
    payment_status = to_lower(info.get("payment_status"))
    order_status = to_lower(info.get("status"))
    order_id = info.get("order_id")

    # -------- 1) Se virou pago, só marca e sai --------
    # Aqui você pode ajustar os códigos conforme observar nos logs
    is_paid = (
        "order.paid" in event
        or payment_status in {"paid", "pago", "3", "aprovado", "approved"}
        or order_status in {"paid", "pago"}
    )

    if is_paid:
        if order_id is not None:
            paid_orders.add(order_id)
        log.info(
            f"[{order_id}] marcado como pago (event={event}, "
            f"method={payment_method}, status={payment_status})"
        )
        return JSONResponse(
            {
                "ok": True,
                "action": "marked_paid",
                "order_id": order_id,
            }
        )

    # -------- 2) Se for PIX e ainda pendente, agenda job pra 5min --------
    is_pix = payment_method.startswith("pix")

    # No CartPanda, pelo seu log, payment_status=1 é pendente / aguardando
    is_pending = (
        payment_status in {"pending", "pendente", "aguardando", "1", "0"}
        or order_status in {"new", "open", "pending", "pendente", "aguardando"}
    )

    if is_pix and ("order.created" in event or is_pending):
        # agenda checagem para daqui 5 minutos
        run_at = datetime.utcnow() + timedelta(minutes=5)

        try:
            scheduler.add_job(
                check_and_send_if_still_pending,
                "date",
                run_date=run_at,
                id=f"pix_{order_id}",
                replace_existing=True,
                args=[
                    order_id,
                    info.get("name"),
                    info.get("phone"),
                    info.get("product"),
                    info.get("price"),
                    info.get("checkout_url"),
                ],
            )
            log.info(
                f"[{order_id}] PIX pendente detectado; job agendado para "
                f"{run_at.isoformat()} UTC "
                f"(event={event}, status={payment_status})"
            )
            return JSONResponse(
                {
                    "ok": True,
                    "action": "scheduled_5min_check",
                    "order_id": order_id,
                }
            )
        except Exception as e:
            log.exception(f"[{order_id}] erro ao agendar job de 5min: {e}")
            return JSONResponse(
                {
                    "ok": False,
                    "action": "schedule_error",
                    "error": str(e),
                    "order_id": order_id,
                }
            )

    # -------- 3) Qualquer outra coisa é ignorada --------
    log.info(
        f"[{order_id}] ignorado "
        f"(event={event}, method={payment_method}, status={payment_status})"
    )
    return JSONResponse(
        {
            "ok": True,
            "action": "ignored",
            "order_id": order_id,
        }
    )


# ---------------- Health & favicon ----------------
@app.get("/")
def root():
    return {"ok": True, "service": "pix-whatsapp-automation"}


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/favicon.ico")
@app.get("/favicon.png")
def favicon():
    # só pra não ficar jogando 404 no log
    return Response(status_code=204)


# ---------------- Startup do scheduler ----------------
@app.on_event("startup")
def _start_scheduler():
    if not scheduler.running:
        log.info("Iniciando scheduler em background...")
        scheduler.start()
