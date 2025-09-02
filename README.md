# CartPanda PIX pendente → WhatsApp (5 min)

## Como funciona
- Recebe webhooks do CartPanda.
- Se `payment_status=pending` e `gateway=pix`, agenda disparo via WhatsApp em **+5 min**.
- Se dentro desse período chegar `payment_status=paid`, o job é **cancelado**.
- Se não chegar, envia mensagem via **Z-API**.

## Endpoints
- `GET /healthz` → status
- `POST /webhook/cartpanda` → recebe os eventos

### Payload esperado
```json
{
  "order_id": "123",
  "payment_status": "pending",
  "gateway": "pix",
  "customer": {"name": "Maria", "phone": "+5511999999999"},
  "product": {"title": "Tabib 2025", "price": 9.9, "checkout_url": "https://.../checkout/xyz"}
}
```

## Rodar local
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Testes com curl
### 1) Agendar
```bash
curl -X POST http://localhost:8000/webhook/cartpanda \
  -H "Content-Type: application/json" \
  -d '{"order_id":"abc123","payment_status":"pending","gateway":"pix","customer":{"name":"Maria","phone":"+5511999999999"},"product":{"title":"Tabib 2025","price":9.9,"checkout_url":"https://checkout"}}'
```
→ resposta: `{"scheduled": true, "order_id": "abc123", "in_minutes": 5}`

### 2) Cancelar
```bash
curl -X POST http://localhost:8000/webhook/cartpanda \
  -H "Content-Type: application/json" \
  -d '{"order_id":"abc123","payment_status":"paid","gateway":"pix"}'
```
→ resposta: `{"canceled": true, "order_id": "abc123"}`

## Deploy no Render
- Start command: `uvicorn main:app --host 0.0.0.0 --port 10000`
- Defina as variáveis de ambiente do `.env.sample`.
- Plano com **no sleep** recomendado para não perder jobs.

## Observações
- Este MVP usa memória do servidor. Para produção, use Redis/DB para jobs.
- O endpoint exato da Z-API varia por provedor. Ajuste `ZAPI_BASE_URL` se necessário.
