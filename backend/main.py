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
import time, json, os, asyncio, logging
from functools import lru_cache

try:
    from openai import AsyncOpenAI  # type: ignore
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("limbic.backend")

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

# 兼容 /health (有些反向代理或监控默认探活路径) 不写入 OpenAPI 文档
@app.get("/health", include_in_schema=False)
def health_alias():
    return healthz()


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest, request: Request):
    """OpenAI 直连流式接口，符合前端 /api/chat 转发预期。

    返回 SSE 风格：data: {json}\n  末尾 data: [DONE]\n
    """
    logger.info("/chat/stream request: messages=%d model=%s", len(body.messages), body.model or "<default>")
    try:
        client, ocfg = get_openai_client()
    except Exception as e:
        logger.error("config/openai init error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)

    # 模型名归一化：前端如果传了 JSON schema 默认 "string" / "model" / 空等占位，回退到配置默认
    invalid_placeholders = {None, "", "string", "model", "undefined", "null"}
    raw_model = (body.model or "").strip() if body.model else ""
    if raw_model.lower() in invalid_placeholders:
        model = ocfg.get("model", "gpt-4o-mini")
        logger.info("model placeholder '%s' -> fallback '%s'", raw_model or "<empty>", model)
    else:
        model = raw_model
    temperature = body.temperature if body.temperature is not None else ocfg.get("temperature", 0.7)
    max_tokens = body.max_tokens if body.max_tokens is not None else ocfg.get("max_output_tokens") or None

    # Normalize roles
    msgs = []
    for m in body.messages:
        role = m.role if m.role in {"user", "assistant", "system"} else "user"
        msgs.append({"role": role, "content": m.content})

    async def gen() -> AsyncGenerator[bytes, None]:
        # 立即发送一个起始空增量，避免前端长时间无字节误判超时 / 移除图标
        start_payload = {"choices": [{"delta": {"content": ""}}], "__start": True}
        yield f"data: {json.dumps(start_payload, ensure_ascii=False)}\n".encode("utf-8")
        # 若仍然是占位或缺失，提前给出错误避免调用 404
        if not model or model.lower() in invalid_placeholders:
            err_payload = {
                "error": "model not configured (server fallback also empty)",
                "choices": [{"delta": {"content": "[ERROR] model not configured"}}]
            }
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n".encode("utf-8")
            yield b"data: [DONE]\n"
            return
        try:
            logger.info("creating openai stream model=%s temp=%s max_tokens=%s", model, temperature, max_tokens)
            stream = await client.chat.completions.create(  # type: ignore
                model=model,
                messages=msgs,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
            got_any = False
            async for chunk in stream:  # type: ignore
                if await request.is_disconnected():
                    logger.info("client disconnected during stream")
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
                        got_any = True
                except Exception as inner_e:
                    logger.warning("stream chunk parse error: %s", inner_e)
                    continue
            if not got_any:
                logger.warning("openai stream returned no content pieces (empty response)")
            yield b"data: [DONE]\n"
        except Exception as e:
            logger.error("openai streaming error: %s", e)
            # 以兼容格式发回错误（choices 结构 + error 字段）便于前端显示
            err_payload = {
                "error": str(e),
                "choices": [{"delta": {"content": f"[ERROR] {str(e)}"}}]
            }
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n".encode("utf-8")
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
