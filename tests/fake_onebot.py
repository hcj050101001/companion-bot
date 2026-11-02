"""Standalone fake OneBot v11 HTTP service and event fixture for tests."""

import hashlib
import hmac
import json

import uvicorn
from fastapi import FastAPI, Request


def make_fake_onebot(secret: str):
    app = FastAPI()
    app.state.calls = []
    app.state.events = []

    @app.get("/get_status")
    async def get_status():
        return {"online": True}

    @app.post("/send_private_msg")
    async def send_private_msg(request: Request):
        payload = await request.json()
        app.state.calls.append(payload)
        return {"status": "ok"}

    @app.post("/inject_event")
    async def inject_event(request: Request):
        payload = await request.json()
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        signature = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
        app.state.events.append({"body": body, "signature": signature})
        # In-process ASGI tests can inspect the signed payload. A standalone
        # service also offers /onebot/event, which forwards the event itself.
        return {"body": body.decode(), "signature": signature}

    @app.post("/onebot/event")
    async def event_sink(request: Request):
        body = await request.body()
        app.state.events.append({"body": body, "signature": request.headers.get("X-Signature")})
        return {"status": "ok"}

    return app


app = make_fake_onebot("fake-secret")


if __name__ == "__main__":
    uvicorn.run("tests.fake_onebot:app", host="127.0.0.1", port=3100)


def private_event(user_id: str = "10001", text: str = "测试消息") -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        # 真实 NapCat 报文恒带 self_id；P2 起接受边界要求 self_id 与
        # message_id 俱在（KD-C02），合成夹具与真实报文对齐。
        "self_id": "900000001",
        "user_id": user_id,
        "message": text,
        "raw_message": text,
    }
