from fastapi import FastAPI, Request, HTTPException
import os

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/webhook/mercadopago")
async def mp_webhook(request: Request):
    # тут позже будет проверка подписи + mark_payment_paid + grant_subscription
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="MP_ACCESS_TOKEN missing")
    payload = await request.json()
    return {"ok": True, "received": payload}
