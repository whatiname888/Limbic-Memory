"""Minimal temporary FastAPI backend for Limbic Memory testing.

Endpoints are stubs so that the initialization script (start.sh) can
install dependencies and a simple server can be started for manual testing.

This file is intentionally lightweight and WILL be replaced by the real
memory engine implementation later.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from typing import List, Optional, AsyncGenerator
import time, json, os, asyncio
from functools import lru_cache

try:
    from openai import AsyncOpenAI  # type: ignore
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore

app = FastAPI(title="Limbic Memory Backend", version="0.1.0")


class HealthResponse(BaseModel):
    status: str
    time: float


class ActivateRequest(BaseModel):
    query: str
    top_k: int = 5


class MemoryChunk(BaseModel):
    id: str
    content: str
    score: float
    source: Optional[str] = None


class ActivateResponse(BaseModel):
    query: str
    results: List[MemoryChunk]


class WriteRequest(BaseModel):
    chunks: List[str]


class WriteResponse(BaseModel):
    written: int


# ----------------- Simple OpenAI Chat Support -----------------
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CONFIG_EXAMPLE_PATH = os.path.join(os.path.dirname(__file__), "config.example.json")

@lru_cache(maxsize=1)
def load_config() -> dict:
    path = CONFIG_PATH if os.path.isfile(CONFIG_PATH) else CONFIG_EXAMPLE_PATH
    if not os.path.isfile(path):
        raise RuntimeError("Missing backend/config.json (copy config.example.json and set api_key)")
    data = json.loads(open(path, "r", encoding="utf-8").read())
    return data

def get_openai_client():
    cfg = load_config()
    openai_cfg = cfg.get("openai", {})
    api_key = openai_cfg.get("api_key", "")
    if not api_key or api_key.startswith("sk-REPLACE"):
        raise RuntimeError("OpenAI api_key not set in backend/config.json")
    if AsyncOpenAI is None:
        raise RuntimeError("openai package not installed")
    base_url = openai_cfg.get("base_url") or None
    return AsyncOpenAI(api_key=api_key, base_url=base_url), openai_cfg


@app.get("/healthz", response_model=HealthResponse, summary="Health check")
def healthz():
    return HealthResponse(status="ok", time=time.time())


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request):
    """OpenAI 直连流式接口，符合前端 /api/chat 转发预期。

    返回 SSE 风格：data: {json}\n  末尾 data: [DONE]\n
    """
    try:
        client, ocfg = get_openai_client()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    model = body.model or ocfg.get("model", "gpt-4o-mini")
    temperature = body.temperature if body.temperature is not None else ocfg.get("temperature", 0.7)
    max_tokens = body.max_tokens if body.max_tokens is not None else ocfg.get("max_output_tokens") or None

    # Normalize roles
    msgs = []
    for m in body.messages:
        role = m.role if m.role in {"user", "assistant", "system"} else "user"
        msgs.append({"role": role, "content": m.content})

    async def gen() -> AsyncGenerator[bytes, None]:
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            async for chunk in stream:  # type: ignore
                if await request.is_disconnected():
                    break
                try:
                    choice = chunk.choices[0]
                    piece = None
                    delta = getattr(choice, "delta", None)
                    if delta and getattr(delta, "content", None):
                        piece = delta.content
                    elif getattr(choice, "message", None) and getattr(choice.message, "content", None):
                        piece = choice.message.content
                    if piece:
                        payload = {"choices": [{"delta": {"content": piece}}]}
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n".encode("utf-8")
                except Exception:
                    continue
            yield b"data: [DONE]\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n".encode("utf-8")
            yield b"data: [DONE]\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/memory/activate", response_model=ActivateResponse, summary="Stub activation retrieval")
def activate(req: ActivateRequest):
    # Placeholder: returns synthetic results for now
    fake = [
        MemoryChunk(id=f"stub-{i}", content=f"Stub memory chunk {i} for '{req.query}'", score=1.0 - i * 0.05)
        for i in range(req.top_k)
    ]
    return ActivateResponse(query=req.query, results=fake)


@app.post("/memory/write", response_model=WriteResponse, summary="Stub write endpoint")
def write(req: WriteRequest):
    # Placeholder simply counts chunks
    return WriteResponse(written=len(req.chunks))


@app.get("/memory/debug/all", summary="List synthetic in-memory store (empty)")
def debug_all():
    # No real store yet
    return {"data": [], "note": "No real store implemented yet"}


# Convenience root
@app.get("/")
def root():
    return {"service": "limbic-memory-temp", "endpoints": ["/healthz", "/memory/activate", "/memory/write"], "note": "Temporary backend stub"}
