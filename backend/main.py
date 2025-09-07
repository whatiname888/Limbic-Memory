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
import time, json, os, asyncio, logging, uuid
from functools import lru_cache
import re

try:
    from openai import AsyncOpenAI  # type: ignore
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore
try:
    import chromadb  # type: ignore
    from chromadb.config import Settings as ChromaSettings  # type: ignore
except Exception:  # pragma: no cover
    chromadb = None  # type: ignore

VECTOR_DIR = os.path.join(os.path.dirname(__file__), "vector_db")
os.makedirs(VECTOR_DIR, exist_ok=True)
SHORT_MEM_DIR = os.path.join(os.path.dirname(__file__), "short_mem")
os.makedirs(SHORT_MEM_DIR, exist_ok=True)
SHORT_MEM_FILE = os.path.join(SHORT_MEM_DIR, "current.json")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("limbic.backend")

app = FastAPI(title="Limbic Memory Backend", version="0.2.0")


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
    ids: List[str] = []


# ----------------- Simple OpenAI Chat Support -----------------
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    enable_memory_tools: bool = True  # 是否启用模型驱动的记忆工具调用

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
CONFIG_EXAMPLE_PATH = os.path.join(os.path.dirname(__file__), "config.example.json")

@lru_cache(maxsize=1)
def load_config() -> dict:
    path = CONFIG_PATH if os.path.isfile(CONFIG_PATH) else CONFIG_EXAMPLE_PATH
    if not os.path.isfile(path):
        raise RuntimeError("Missing backend/config.json (copy config.example.json and set api_key)")
    data = json.loads(open(path, "r", encoding="utf-8").read())
    return data

def _build_client(cfg_section: dict, section_name: str):
    api_key = cfg_section.get("api_key", "")
    if not api_key or api_key.startswith("sk-REPLACE"):
        raise RuntimeError(f"{section_name} api_key not set in backend/config.json")
    if AsyncOpenAI is None:
        raise RuntimeError("openai package not installed")
    base_url = cfg_section.get("base_url") or None
    return AsyncOpenAI(api_key=api_key, base_url=base_url)

@lru_cache(maxsize=1)
def get_chat_client():
    cfg = load_config()
    # 优先 chat 节点，向后兼容旧 openai 节点
    chat_cfg = cfg.get("chat") or cfg.get("openai") or {}
    client = _build_client(chat_cfg, "chat")
    return client, chat_cfg

@lru_cache(maxsize=1)
def get_embedding_client():
    cfg = load_config()
    emb_cfg = cfg.get("embedding") or cfg.get("chat") or cfg.get("openai") or {}
    client = _build_client(emb_cfg, "embedding")
    return client, emb_cfg

@lru_cache(maxsize=1)
def get_embedding_dimension() -> int:
    cfg = load_config()
    emb_cfg = cfg.get("embedding") or {}
    dim = emb_cfg.get("dimension") or emb_cfg.get("dim") or 1536
    try:
        dim_int = int(dim)
        if dim_int <= 0:
            raise ValueError
        return dim_int
    except Exception:
        return 1536

# --------------- Hippocampus Client ---------------
@lru_cache(maxsize=1)
def get_hippocampus_client():
    cfg = load_config()
    hip_cfg = cfg.get("hippocampus") or {}
    if not hip_cfg:
        raise RuntimeError("hippocampus config missing")
    client = _build_client(hip_cfg, "hippocampus")
    return client, hip_cfg

def _load_short_mem() -> dict:
    if not os.path.isfile(SHORT_MEM_FILE):
        return {
            "version": 1,
            "background": "",
            "stm": [],  # 压缩后的层级或条目
            "raw_history": [],
            "last_updated": time.time()
        }
    try:
        return json.loads(open(SHORT_MEM_FILE, 'r', encoding='utf-8').read())
    except Exception:
        return {"version":1, "background":"", "stm":[], "raw_history":[], "last_updated": time.time()}

def _save_short_mem(data: dict):
    data["last_updated"] = time.time()
    with open(SHORT_MEM_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def build_short_mem_context() -> Optional[str]:
    """仅构造需要以 system 形式注入的背景摘要文本。

    变更: 不再把最近对话条目合并为一个 system 块；最近原始对话将以正常 user/assistant 角色注入，避免被全部视作系统指令，从而更贴近真实对话语境。
    返回值: 若无 background 则返回 None。
    """
    try:
        cfg = load_config()
        hip = cfg.get("hippocampus", {})
        if not hip or not hip.get("inject_context", True):
            return None
        # 仅返回背景摘要，明确这是“对用户的描述”而非对模型的再设定，降低角色混淆风险
        prefix = hip.get("inject_prefix", "[会话背景摘要]")
        sm = _load_short_mem()
        background = (sm.get("background") or "").strip()
        if not background:
            return None
        disclaimer = "(说明: 下方为对【用户】过往对话的聚合摘要, 用于连续性参考。它描述的是用户而不是你本身, 不要把其中第一人称当作你自己的话; 仅在需要时用于理解与推理, 不要逐句复述给用户。)"
        lines = [prefix + " " + disclaimer, background[:600]]
        text = '\n'.join(lines)
        if len(text) > 3000:
            text = text[:2950] + '\n...(截断)'
        return text
    except Exception as e:
        logger.warning("build_short_mem_context failed: %s", e)
        return None

async def run_hippocampus_transform(new_user_text: str) -> dict:
    """调用海马体 LLM，对短期记忆进行结构化转换，并返回报告。

    期望模型输出（任意一种即可解析）：
    ```json
    {"background": "...", "stm": [...], "ltm_candidates": ["..."]}
    ```
    若缺失字段使用旧值或空值。
    """
    try:
        client, hip_cfg = get_hippocampus_client()
    except Exception as e:
        logger.info("hippocampus disabled: %s", e)
        return {"enabled": False}
    sm = _load_short_mem()
    # 追加最新用户消息（不再做任何裁剪/截断；完整短期对话完全由海马体负责语义整合，但原始 raw_history 永久保留）
    sm['raw_history'].append({"role": "user", "content": new_user_text, "ts": time.time()})
    enable_compression = hip_cfg.get("enable_compression", True)
    compression_prompt = hip_cfg.get("compression_prompt")
    # 不裁剪, to_prune 恒为空 -> 若未配置自定义 compression_prompt 则直接跳过压缩
    to_prune: List[dict] = []
    system_prompt = hip_cfg.get("system_prompt") or (
        "你是海马体记忆整合模块。输入: 最近用户消息 + 现有 background + stm(可为空)。输出 JSON: {background, stm, ltm_candidates}.\n"
        "规则: background 为滚动语义摘要(不含细节对话原文, 但保留长期人物/目标/偏好/情绪模式); stm 为分层或列表条目, 最新内容更原始; ltm_candidates 为需写入长期记忆的独立文本数组, 每条独立完整自然句。不要返回除 JSON 以外的多余说明。"
    )
    # 组装输入消息
    # 构造给压缩模型的输入：background + recent + to_prune（只在有需要时）
    recent_dialog = []
    for r in sm.get('raw_history', [])[-min(len(sm.get('raw_history', [])), 20):]:  # 最近 20 条用于上下文参考
        if isinstance(r, dict) and r.get('role') in {"user","assistant"}:
            recent_dialog.append(f"{r['role']}: {r.get('content','')}")
    compression_input = {
        "background": sm.get("background", ""),
        "recent": recent_dialog,
        "to_prune": [f"{r.get('role')}: {r.get('content','')}" for r in to_prune if isinstance(r, dict) and r.get('role') in {"user","assistant"}]
    }
    if not enable_compression or (not to_prune and not compression_prompt):
        # 不进行压缩，直接保存并返回，无新增长期写入
        _save_short_mem(sm)
        return {"enabled": True, "background": sm.get("background",""), "stm_size": len(sm.get("stm", [])), "ltm_written": [], "ltm_candidate_count": 0, "raw_output": ""}
    hip_messages = [
        {"role": "system", "content": compression_prompt or system_prompt},
        {"role": "user", "content": json.dumps(compression_input, ensure_ascii=False)}
    ]
    model = hip_cfg.get("model") or hip_cfg.get("fallback_model") or "gpt-4o-mini"
    temperature = hip_cfg.get("temperature", 0.2)
    try:
        resp = await client.chat.completions.create(  # type: ignore
            model=model,
            messages=hip_messages,
            temperature=temperature,
            stream=False
        )
        content = resp.choices[0].message.content if resp.choices else ""
    except Exception as e:
        logger.warning("hippocampus call failed: %s", e)
        return {"enabled": True, "error": str(e)}
    # 解析 JSON（支持 fenced code）
    parsed = {}
    if content:
        m = re.search(r"```json\s*(\{[\s\S]+?\})\s*```", content)
        raw_json = None
        if m:
            raw_json = m.group(1)
        else:
            # 尝试直接整体解析
            if content.strip().startswith('{') and content.strip().endswith('}'):
                raw_json = content.strip()
        if raw_json:
            try:
                parsed = json.loads(raw_json)
            except Exception:
                parsed = {}
    background_new = parsed.get('background') if isinstance(parsed.get('background'), str) else sm.get('background','')
    stm_new = sm.get('stm', [])  # 不再更新 stm（逐步废弃）
    ltm_candidates = parsed.get('ltm_candidates') if isinstance(parsed.get('ltm_candidates'), list) else []
    # 写入长期记忆（向量库）
    written = []
    for cand in ltm_candidates:
        if not isinstance(cand, str) or not cand.strip():
            continue
        try:
            res = await tool_memory_store(cand.strip())
            written.append({"text": cand.strip()[:180], "id": res.get('id')})
        except Exception as e:
            written.append({"text": cand.strip()[:180], "error": str(e)})
    # 更新短期记忆文件
    sm['background'] = background_new
    sm['stm'] = stm_new  # 保留占位，未来可移除
    _save_short_mem(sm)
    return {
        "enabled": True,
        "background": background_new,
        "stm_size": len(stm_new),
    "ltm_written": written,
    "ltm_candidate_count": len(ltm_candidates),
        "raw_output": content
    }


# --------------- Vector Memory (Chroma) ---------------
@lru_cache(maxsize=1)
def get_chroma():
    if chromadb is None:
        raise RuntimeError("chromadb not installed. Re-run install after adding requirement.")
    cfg = load_config()
    mem_cfg = cfg.get("memory", {})
    collection = mem_cfg.get("collection", "long_term_memory")
    client = chromadb.PersistentClient(path=VECTOR_DIR, settings=ChromaSettings(anonymized_telemetry=False))
    coll = client.get_or_create_collection(name=collection)
    return coll

async def embed_texts(texts: List[str]) -> List[List[float]]:
    # Use OpenAI embedding endpoint for simplicity; fallback to zeros if failure.
    try:
        client, cfg = get_embedding_client()
        # embedding 节点: model；兼容旧 chat/openai fallback
        root_cfg = load_config()
        model = cfg.get("model") or root_cfg.get("embedding_model") or "text-embedding-3-small"
        resp = await client.embeddings.create(model=model, input=texts)  # type: ignore
        return [d.embedding for d in resp.data]  # type: ignore
    except Exception as e:  # fallback deterministic
        logger.warning("embedding failed, fallback zeros: %s", e)
        dim = get_embedding_dimension()
        return [[0.0]*dim for _ in texts]

def format_tool_markdown(steps: List[dict]) -> str:
    if not steps:
        return ""
    # 特殊处理：首条为自动召回 memory_recall (auto) 时单独以“记忆激活”标题展示
    first_auto = False
    if steps and isinstance(steps[0].get('name'), str) and 'memory_recall' in steps[0]['name'] and '(auto)' in steps[0]['name']:
        first_auto = True
    lines: List[str] = []
    if first_auto:
        s0 = steps[0]
        lines.append("### 记忆激活")
        lines.append("")
        # 展示自动激活的查询参数与结果摘要
        lines.append("**来源**: 自动检索 (auto_query_recall)")
        if 'arguments' in s0:
            try:
                lines.append(f"- 查询: `{json.dumps(s0['arguments'], ensure_ascii=False)[:280]}`")
            except Exception:
                pass
        if 'result_preview' in s0:
            lines.append(f"- 结果: {s0['result_preview']}")
        if 'duration_ms' in s0:
            lines.append(f"- 耗时: {s0['duration_ms']} ms")
        lines.append("")
        remaining = steps[1:]
        if remaining:
            lines.append("### 工具调用步骤 (模型真实触发)")
            lines.append("")
            for idx, s in enumerate(remaining, 1):
                lines.append(f"**步骤 {idx}: {s.get('name','tool')}**")
                if 'arguments' in s:
                    try:
                        lines.append(f"- 参数: `{json.dumps(s['arguments'], ensure_ascii=False)[:280]}`")
                    except Exception:
                        pass
                if 'result_preview' in s:
                    lines.append(f"- 返回: {s['result_preview']}")
                if 'duration_ms' in s:
                    lines.append(f"- 耗时: {s['duration_ms']} ms")
                lines.append("")
    else:
        lines.append("### 工具调用步骤 (模型真实触发)")
        lines.append("")
        for i, s in enumerate(steps, 1):
            lines.append(f"**步骤 {i}: {s.get('name','tool')}**")
            if 'arguments' in s:
                try:
                    lines.append(f"- 参数: `{json.dumps(s['arguments'], ensure_ascii=False)[:280]}`")
                except Exception:
                    pass
            if 'result_preview' in s:
                lines.append(f"- 返回: {s['result_preview']}")
            if 'duration_ms' in s:
                lines.append(f"- 耗时: {s['duration_ms']} ms")
            lines.append("")
    lines.append("---\n")
    return "\n".join(lines)

# --------- Memory Tools ---------
async def tool_memory_store(text: str) -> dict:
    try:
        coll = get_chroma()
        emb = await embed_texts([text])
        mid = str(uuid.uuid4())
        coll.add(ids=[mid], documents=[text], embeddings=emb)
        return {"id": mid, "length": len(text)}
    except Exception as e:
        return {"error": str(e)}

async def tool_memory_recall(query: str, top_k: Optional[int] = None) -> dict:
    try:
        cfg = load_config()
        mem_cfg = cfg.get("memory", {})
        k = top_k or mem_cfg.get("top_k", 5)
        coll = get_chroma()
        emb = await embed_texts([query])
        res = coll.query(query_embeddings=emb, n_results=k)
        docs = res.get('documents') or []
        ids = res.get('ids') or []
        flat = []
        for gi, group in enumerate(docs):
            for ji, doc in enumerate(group):
                _id = ids[gi][ji] if gi < len(ids) and ji < len(ids[gi]) else ""
                flat.append({"id": _id, "content": doc})
        return {"results": flat[:k], "count": len(flat)}
    except Exception as e:
        return {"error": str(e)}

def build_tool_spec():
    return [
        {"type": "function", "function": {"name": "memory_store", "description": "存储新的长期记忆文本", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}},
        {"type": "function", "function": {"name": "memory_recall", "description": "检索相关记忆", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer", "default": 5}}, "required": ["query"]}}}
    ]

INLINE_TOOL_PATTERN = re.compile(r"^\s*\{\s*\"name\"\s*:\s*\"(memory_store|memory_recall)\"", re.IGNORECASE)

def _try_parse_inline_tool(content: str):
    if not content or not INLINE_TOOL_PATTERN.search(content):
        return None
    try:
        data = json.loads(content)
        name = data.get("name")
        args = data.get("arguments") or {}
        if name in {"memory_store", "memory_recall"}:
            return name, args
    except Exception:
        return None
    return None

async def run_tool_iteration(client, model_name: str, messages, tools):
    executed: List[dict] = []
    cfg = load_config()
    mem_cfg = cfg.get("memory", {})
    loops_cfg = mem_cfg.get("max_tool_loops", 8)
    # 安全上限防止死循环
    loops = min(int(loops_cfg) if isinstance(loops_cfg, (int, float, str)) and str(loops_cfg).isdigit() else 8, 30)
    for loop_idx in range(loops):
        try:
            resp = await client.chat.completions.create(  # type: ignore
                model=model_name,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.2,
                stream=False,
            )
        except Exception as e:
            logger.warning("tool planning call failed(loop %s): %s", loop_idx, e)
            break
        choice = resp.choices[0]
        msg = choice.message
        tool_calls = getattr(msg, 'tool_calls', None)
        inline_call = None
        if not tool_calls and getattr(msg, 'content', None):
            inline_call = _try_parse_inline_tool(msg.content)
        if not tool_calls and not inline_call:
            # 没有更多工具调用 -> 追加最终 assistant 内容（自然语言）
            if getattr(msg, 'content', None):
                messages.append({"role": "assistant", "content": msg.content})
            break
        # 记录模型决定调用的工具（assistant message）
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": tc.type, "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ]
            })
        elif inline_call:
            # 模型直接输出 JSON 形式 {"name":"memory_store", "arguments":{...}}
            name, arguments = inline_call
            # 构造伪 tool_call id 以统一后续处理
            fake_id = str(uuid.uuid4())
            messages.append({
                "role": "assistant",
                "content": "",  # 不把 JSON 原样留给后续回答
                "tool_calls": [{
                    "id": fake_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}
                }]
            })
            # 构造一个模拟的对象接口结构
            class FakeFunc:  # minimal shim
                def __init__(self, name, arguments):
                    self.name = name
                    self.arguments = json.dumps(arguments, ensure_ascii=False)
            class FakeTC:
                def __init__(self, _id, name, args):
                    self.id = _id
                    self.type = 'function'
                    self.function = FakeFunc(name, args)
            tool_calls = [FakeTC(fake_id, name, arguments)]
        # 逐个执行工具
        for tc in tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments or '{}'
            try:
                parsed = json.loads(raw_args)
            except Exception:
                parsed = {}
            t0 = time.time()
            if name == 'memory_store':
                result = await tool_memory_store(parsed.get('text',''))
            elif name == 'memory_recall':
                result = await tool_memory_recall(parsed.get('query',''), parsed.get('top_k'))
            else:
                result = {"error": f"unknown tool {name}"}
            dur = int((time.time() - t0) * 1000)
            preview = json.dumps(result, ensure_ascii=False)[:500]
            executed.append({
                "name": name,
                "arguments": parsed,
                "result_preview": preview,
                "duration_ms": dur
            })
            # 工具结果消息（模型下一轮可读取）
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": json.dumps(result, ensure_ascii=False)
            })
    return messages, executed


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
        client, ocfg = get_chat_client()
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

    # Normalize roles & prepend configurable system prompt and injected memory/background
    msgs = []
    base_system = ocfg.get("system_prompt")
    if base_system:
        msgs.append({"role": "system", "content": base_system})
    # 仅以 system 注入背景摘要
    sm_background = build_short_mem_context()
    if sm_background:
        msgs.append({"role": "system", "content": sm_background})
    # 以正常 user/assistant 角色注入全部当前短期记忆 raw_history（海马体负责长度控制）
    try:
        sm_all = _load_short_mem()
        rh_all = sm_all.get("raw_history") or []
        seq = [r for r in rh_all if isinstance(r, dict) and r.get("role") in {"user","assistant"} and r.get("content")]
        existing_pairs = {(m.role, m.content) for m in body.messages}
        injected = 0
        injected_raw_indices = []  # 记录索引用于后续避免被 max_history 截断
        for r in seq:
            tup = (r.get("role"), r.get("content"))
            if tup in existing_pairs:
                continue
            msgs.append({"role": r.get("role"), "content": r.get("content"), "_injected_raw": True})
            injected_raw_indices.append(len(msgs)-1)
            injected += 1
        logger.info("[raw-history-inject] mode=ALL injected=%d total_raw=%d", injected, len(seq))
        if seq:
            logger.info("[raw-history-first] role=%s len=%d preview=%s", seq[0].get('role'), len(seq[0].get('content','')), seq[0].get('content','')[:80].replace('\n',' '))
    except Exception as inj_e:
        logger.warning("inject raw history messages failed: %s", inj_e)
    if body.enable_memory_tools:
        tool_instruction = (
            "[记忆工具精简指令]\n"
            "工具: memory_recall(query, top_k?), memory_store(text)\n"
            "优先: 几乎每次答复前先 memory_recall；若无结果 -> 简短说明暂无记录并邀请补充。\n"
            "存储触发: 发现新的且可能在后续仍有价值的 ‘身份/偏好/长期目标/持续情绪或行为模式/仪式习惯’ 就调用 memory_store；一次用户输入可多条。\n"
            "存储格式: 自然语言完整句式 (日记风格)，包含主体+要点+动机/背景，不写碎片。\n"
            "禁止: 回复里直接出现 memory_recall(...) / memory_store(...) 或 JSON；工具只在内部。\n"
            "稳健: 绝不臆造用户未表达的事实；不确定就先问，再存。\n"
            "回答整合: 工具后给用户自然语言共情+整合，必要时引用已知记忆要点。\n"
            "鼓励: 若犹豫是否存且信息具长期特征 -> 倾向于存。"
        )
        msgs.append({"role": "system", "content": tool_instruction})
    for m in body.messages:
        role = m.role if m.role in {"user", "assistant", "system"} else "user"
        msgs.append({"role": role, "content": m.content})
    # 限制对话历史条数（不含我们在上面插入的 system 注入），只裁剪用户/assistant/system的原始对话部分
    try:
        max_hist = int(ocfg.get("max_history_messages", 0) or 0)
        if max_hist > 0:
            # 若存在注入的 raw 历史，则跳过裁剪，防止意外删除。可通过设置 max_history_messages<=0 或后续新增配置控制。
            has_injected = any(isinstance(m, dict) and m.get("_injected_raw") for m in msgs)
            if has_injected:
                logger.info("[trim-skip] detected injected raw history -> skip max_history_messages=%s trimming", max_hist)
            else:
                prefix_count = 0
                for mm in msgs:
                    if mm["role"] == "system" and (mm["content"].startswith("[记忆工具精简指令]") or mm["content"].startswith("I. 核心身份") or mm["content"].startswith("[短期记忆注入") or mm["content"].startswith("[短期记忆注入]")):
                        prefix_count += 1
                    else:
                        break
                head = msgs[:prefix_count]
                rest = msgs[prefix_count:]
                if len(rest) > max_hist:
                    rest = rest[-max_hist:]
                msgs = head + rest
    except Exception as e:
        logger.warning("trim history failed: %s", e)

    # ---------- 上下文预算控制（防止提供商自身裁剪导致最早消息丢失） ----------
    def enforce_context_budget(messages: list):
        try:
            cfg_all = load_config()
            hip_cfg = cfg_all.get("hippocampus", {}) if isinstance(cfg_all, dict) else {}
            budget_chars = int(hip_cfg.get("inject_char_budget", 0) or 0)
            if budget_chars <= 0:
                return messages  # 不启用预算
            # 仅统计 user/assistant 注入内容(含 _injected_raw) + 原始对话，不包含 system 指令
            # 找出按出现顺序的 injected_raw 列表索引
            injected_indices = [i for i,m in enumerate(messages) if isinstance(m, dict) and m.get("_injected_raw")]
            if not injected_indices:
                return messages
            # pin 最早 N 条
            pin_first = int(hip_cfg.get("inject_pin_first", 2) or 2)
            pin_first = max(0, min(pin_first, len(injected_indices)))
            total_chars = 0
            per_index_len = {}
            for i,m in enumerate(messages):
                if m.get("role") in {"user","assistant"}:
                    l = len(m.get("content") or "")
                    total_chars += l
                    per_index_len[i] = l
            if total_chars <= budget_chars:
                logger.info("[budget] total_chars=%d <= budget=%d (no trim)", total_chars, budget_chars)
                return messages
            # 保留策略：保留 pinned earliest injected_raw + 所有近期对话(倒序累计直到满足预算)；删除中间多余 injected_raw
            # 先选出必须保留集合
            pinned_set = set(injected_indices[:pin_first])
            # 从结尾向前累计直到预算满足（包含所有 system + pinned）
            keep_set = set(pinned_set)
            running = 0
            # 预先加上 system 消息长度（都保留）
            for i,m in enumerate(messages):
                if m.get("role") == "system":
                    running += len(m.get("content") or "")
                    keep_set.add(i)
            # 倒序遍历，保留最近消息
            for i in range(len(messages)-1, -1, -1):
                if i in keep_set:
                    continue
                m = messages[i]
                if m.get("role") in {"user","assistant"}:
                    l = per_index_len.get(i, len(m.get("content") or ""))
                    if running + l > budget_chars and i not in pinned_set:
                        continue  # 不再加入
                    keep_set.add(i)
                    running += l
                else:
                    keep_set.add(i)
            # 需要删除的 injected_raw 即那些 _injected_raw 且不在 keep_set
            removed_indices = [i for i in injected_indices if i not in keep_set]
            if not removed_indices:
                logger.info("[budget] after selection still within budget running=%d budget=%d", running, budget_chars)
                return messages
            # 汇总被删内容长度和条数，构造一个压缩摘要消息插在 pinned 最后一个之后
            removed_chars = sum(per_index_len.get(i,0) for i in removed_indices)
            removed_count = len(removed_indices)
            first_insert_pos = max(pinned_set) + 1 if pinned_set else 0
            # 构造摘要（简单）
            summary_text = f"[记忆中间段已压缩 {removed_count} 条 ~{removed_chars}字 已汇总于 background]"
            compress_msg = {"role": "system", "content": summary_text, "_compressed_gap": True}
            new_messages = []
            for idx,m in enumerate(messages):
                if idx == first_insert_pos:
                    new_messages.append(compress_msg)
                if idx in removed_indices:
                    continue
                new_messages.append(m)
            logger.info("[budget-trim] removed=%d chars=%d pinned=%d final_len=%d", removed_count, removed_chars, len(pinned_set), len(new_messages))
            return new_messages
        except Exception as be:
            logger.warning("context budget enforcement failed: %s", be)
            return messages

    # 应用预算（仅当配置显式开启 enable_context_budget 才会压缩；默认跳过确保 raw_history 原封不动）
    if ocfg.get("enable_context_budget", False):
        msgs = enforce_context_budget(msgs)
    else:
        logger.info("[budget-skip] enable_context_budget not set -> forwarding full raw_history count=%d", sum(1 for m in msgs if m.get('_injected_raw')))

    executed_steps: List[dict] = []
    # 预先：尝试调用海马体模块（使用最新一条用户消息）构建短期记忆（不阻塞工具调用; 失败忽略）
    latest_user_text = ""
    for m in reversed(body.messages):
        if m.role == 'user' and m.content:
            latest_user_text = m.content
            break
    # 并行启动海马体任务（避免阻塞主模型思考），稍后在输出末尾 await
    hippocampus_report: dict = {}
    hippocampus_task: Optional[asyncio.Task] = None
    if latest_user_text:
        try:
            hippocampus_task = asyncio.create_task(run_hippocampus_transform(latest_user_text))
        except Exception as e:
            hippocampus_report = {"enabled": False, "error": str(e)}
    else:
        hippocampus_report = {"enabled": False, "error": "no_latest_user_message"}
    if body.enable_memory_tools:
        try:
            tools = build_tool_spec()
            msgs, executed_steps = await run_tool_iteration(client, model, msgs, tools)
            # 如果没有任何 recall 执行且配置 auto_query_recall 为 true，则基于最近用户语句自动触发一次 recall
            try:
                cfg_all = load_config()
                force_recall = cfg_all.get("memory",{}).get("auto_query_recall", False)
            except Exception:
                force_recall = False
            has_recall = any(s.get("name") == "memory_recall" for s in executed_steps)
            if force_recall and not has_recall:
                cfg_mem = cfg_all.get("memory", {}) if 'cfg_all' in locals() else {}
                q_max_chars = int(cfg_mem.get("auto_query_recall_query_max_chars", 800) or 800)
                q_top_k = int(cfg_mem.get("auto_query_recall_top_k", cfg_mem.get("top_k",5)) or 5)
                # 以最近一条用户消息内容作为 query
                user_query = ""
                for m in reversed(msgs):
                    if m.get("role") == "user" and m.get("content"):
                        content = m["content"].strip()
                        user_query = content[-q_max_chars:]
                        break
                if user_query.strip():
                    t0 = time.time()
                    recall_result = await tool_memory_recall(user_query, q_top_k)
                    dur = int((time.time()-t0)*1000)
                    executed_steps.append({
                        "name": "memory_recall (auto)",
                        "arguments": {"query": user_query[:200], "top_k": q_top_k},
                        "result_preview": json.dumps(recall_result, ensure_ascii=False)[:500],
                        "duration_ms": dur
                    })
                    fake_id = str(uuid.uuid4())
                    msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": fake_id, "type": "function", "function": {"name": "memory_recall", "arguments": json.dumps({"query": user_query, "top_k": q_top_k}, ensure_ascii=False)}}]})
                    msgs.append({"role": "tool", "tool_call_id": fake_id, "name": "memory_recall", "content": json.dumps(recall_result, ensure_ascii=False)})
            # 结束后如果没有 store 行为但出现明显可存信息（启发式）：用户消息包含 喜欢/爱/目标/想/计划/习惯
            has_store = any(s.get("name") == "memory_store" for s in executed_steps)
            if not has_store:
                # 取最近用户消息，做一个简单启发式
                latest_user = ""
                for m in reversed(msgs):
                    if m.get("role") == "user" and m.get("content"):
                        latest_user = m["content"]
                        break
                if latest_user:
                    if re.search(r"(喜欢|爱|目标|想要|想做|计划|习惯|偏好)", latest_user):
                        # 构造候选存储文本（压缩到 200 字）
                        candidate = latest_user.strip().replace("\n"," ")
                        candidate = candidate[:200]
                        if candidate:
                            t0 = time.time()
                            store_result = await tool_memory_store(candidate)
                            dur = int((time.time()-t0)*1000)
                            executed_steps.append({
                                "name": "memory_store",
                                "arguments": {"text": candidate[:120]},
                                "result_preview": json.dumps(store_result, ensure_ascii=False)[:300],
                                "duration_ms": dur
                            })
                            fake_id2 = str(uuid.uuid4())
                            msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": fake_id2, "type": "function", "function": {"name": "memory_store", "arguments": json.dumps({"text": candidate}, ensure_ascii=False)}}]})
                            msgs.append({"role": "tool", "tool_call_id": fake_id2, "name": "memory_store", "content": json.dumps(store_result, ensure_ascii=False)})
            # 判断是否需要补一个明确的自然语言答复
            if msgs:
                last = msgs[-1]
                need_final = False
                # 情况1：最后是 tool 结果
                if last["role"] == "tool":
                    need_final = True
                # 情况2：最后 assistant 但 content 为空或只有空白
                elif last["role"] == "assistant" and (not last.get("content") or not last["content"].strip()):
                    need_final = True
                # 情况3：最后 assistant 且包含 tool_calls 但缺少自然语言解释
                elif last["role"] == "assistant" and last.get("tool_calls") and (not last.get("content")):
                    need_final = True
                if need_final:
                    # 给模型一个明确指令：基于工具结果继续自然语言回复
                    msgs.append({
                        "role": "system",
                        "content": "以上是内部记忆工具调用结果。请面向用户给出自然语言回应，融合必要记忆，不要再输出 JSON、函数或工具名称。"
                    })
                    try:
                        final_resp = await client.chat.completions.create(  # type: ignore
                            model=model,
                            messages=msgs,
                            temperature=temperature,
                            stream=False
                        )
                        fr_msg = final_resp.choices[0].message
                        if getattr(fr_msg, 'content', None):
                            msgs.append({"role": "assistant", "content": fr_msg.content})
                    except Exception as fe:
                        logger.warning("final natural language fill failed: %s", fe)
        except Exception as e:
            logger.warning("memory tool iteration failed: %s", e)

    async def gen() -> AsyncGenerator[bytes, None]:
        # 立即发送一个起始空增量，避免前端长时间无字节误判超时 / 移除图标
        start_payload = {"choices": [{"delta": {"content": ""}}], "__start": True}
        yield f"data: {json.dumps(start_payload, ensure_ascii=False)}\n".encode("utf-8")
        # 先输出工具步骤 markdown
        md = format_tool_markdown(executed_steps)
        if md:
            for line in md.split('\n'):
                if not line:
                    continue
                payload = {"choices":[{"delta":{"content": line + "\n"}}]}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n".encode("utf-8")
        # 构造海马体总结串（嵌入到同一条 assistant 输出末尾，而不是独立 markdown 区块）
        async def finalize_and_build_hippocampus_summary() -> str:
            nonlocal hippocampus_report
            if hippocampus_task is not None:
                if not hippocampus_task.done():
                    try:
                        hippocampus_report = await asyncio.wait_for(hippocampus_task, timeout=6.0)
                    except asyncio.TimeoutError:
                        hippocampus_report = {"enabled": False, "error": "timeout"}
                    except Exception as he:
                        hippocampus_report = {"enabled": False, "error": str(he)}
                else:
                    try:
                        hippocampus_report = hippocampus_task.result()
                    except Exception as he2:
                        hippocampus_report = {"enabled": False, "error": str(he2)}
            elif not hippocampus_report:
                hippocampus_report = {"enabled": False, "error": "not_started"}
            rep = hippocampus_report or {}
            # 更清晰的多行块格式，仍嵌入同一回答流
            status_msg = "OK" if rep.get("enabled") else (rep.get("error") or "disabled")
            bg_line = "(无)"
            if rep.get("background"):
                bg_line = rep['background'][:160].replace('\n', ' ')
            written_any = rep.get('ltm_written') or []
            write_lines = []
            for w in written_any[:3]:
                if w.get('id'):
                    write_lines.append(f"  - ✅ {w['id']}: {w['text']}")
                else:
                    write_lines.append(f"  - ❌ {w.get('text')}: {w.get('error')}")
            if not write_lines:
                write_lines.append("  - (无)")
            ltm_cnt = rep.get('ltm_candidate_count')
            stm_sz = rep.get('stm_size')
            header = "---\n🧠 记忆整合日志 (海马体)\n"
            core_lines = [
                f"背景摘要: {bg_line}",
                f"候选条目: {ltm_cnt if ltm_cnt is not None else '-'} | STM:{stm_sz if stm_sz is not None else '-'} | 写入:{len(written_any)} | 状态:{status_msg}",
                "写入详情:",
                *write_lines,
                "---"
            ]
            block = header + "\n".join(core_lines)
            # 机器可解析 JSON 追加（可供前端隐藏解析）
            summary_json = {
                "enabled": rep.get("enabled"),
                "error": rep.get("error"),
                "stm_size": rep.get("stm_size"),
                "ltm_candidate_count": rep.get("ltm_candidate_count"),
                "ltm_written": rep.get("ltm_written"),
                "has_background": bool(rep.get("background")),
            }
            json_marker = "<HIPPOCAMPUS>" + json.dumps(summary_json, ensure_ascii=False) + "</HIPPOCAMPUS>"
            return "\n\n" + block + "\n" + json_marker + "\n"
        assistant_output_buf = ""  # 在异常路径也可访问
        # 若仍然是占位或缺失，提前给出错误避免调用 404
        if not model or model.lower() in invalid_placeholders:
            err_payload = {
                "error": "model not configured (server fallback also empty)",
                "choices": [{"delta": {"content": "[ERROR] model not configured"}}]
            }
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n".encode("utf-8")
            # 仍然输出海马体状态（嵌入式）
            summary_tail = await finalize_and_build_hippocampus_summary()
            payload_tail = {"choices":[{"delta":{"content": summary_tail}}]}
            yield f"data: {json.dumps(payload_tail, ensure_ascii=False)}\n".encode('utf-8')
            yield b"data: [DONE]\n"
            return
        # 允许一次内联工具调用拦截
        inline_intercept_used = False
        async def run_stream(current_messages):
            logger.info("creating openai stream model=%s temp=%s max_tokens=%s", model, temperature, max_tokens)
            # 在真正发起流式调用前输出给主模型的消息摘要，便于验证模型上下文
            try:
                summary_list = []
                for i, mm in enumerate(current_messages):
                    if not isinstance(mm, dict):
                        continue
                    role = mm.get("role")
                    content = mm.get("content", "") or ""
                    c_preview = content.replace("\n", " ")
                    truncated = False
                    max_preview = 280
                    if len(c_preview) > max_preview:
                        c_preview = c_preview[:max_preview-1] + "…"
                        truncated = True
                    meta_flags = []
                    if mm.get("_injected_raw"): meta_flags.append("raw")
                    if mm.get("_compressed_gap"): meta_flags.append("gap")
                    summary_list.append({
                        "i": i,
                        "role": role,
                        "len": len(content),
                        "preview": c_preview,
                        "truncated_preview": truncated,
                        "flags": ",".join(meta_flags) if meta_flags else ""
                    })
                logger.info("[model-input] total=%d messages: %s", len(current_messages), json.dumps(summary_list, ensure_ascii=False))
            except Exception as log_e:
                logger.warning("log model input failed: %s", log_e)
            return await client.chat.completions.create(  # type: ignore
                model=model,
                messages=current_messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
            )
        try:
            stream = await run_stream(msgs)
            got_any = False
            buffer_first = ""
            # assistant_output_buf 已在外层定义
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
                    if piece is not None:
                        # 累积前 200 字节检测内联工具 JSON 或函数风格调用
                        if not got_any:
                            buffer_first += piece
                            snippet = buffer_first.strip()
                            inline_json = _try_parse_inline_tool(snippet) if len(snippet) < 600 else None
                            inline_func = None
                            if not inline_json:
                                # 匹配 memory_store("...") 或 memory_recall("...")
                                mfunc = re.match(r"^(memory_store|memory_recall)\(\s*\"(.+?)\"\s*\)\s*$", snippet)
                                if mfunc:
                                    inline_func = (mfunc.group(1), {"text" if mfunc.group(1)=="memory_store" else "query": mfunc.group(2)})
                            if (inline_json or inline_func) and not inline_intercept_used:
                                inline_intercept_used = True
                                name, args = inline_json if inline_json else inline_func
                                # 执行工具
                                t0 = time.time()
                                if name == 'memory_store':
                                    result = await tool_memory_store(args.get('text',''))
                                else:
                                    result = await tool_memory_recall(args.get('query',''), args.get('top_k'))
                                dur = int((time.time()-t0)*1000)
                                executed_steps.append({"name": name, "arguments": args, "result_preview": json.dumps(result, ensure_ascii=False)[:300], "duration_ms": dur})
                                # 将工具结果写入消息并追加 system 指令再重新流式回答
                                fake_tc_id = str(uuid.uuid4())
                                msgs.append({"role": "assistant", "content": "", "tool_calls": [{"id": fake_tc_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]})
                                msgs.append({"role": "tool", "tool_call_id": fake_tc_id, "name": name, "content": json.dumps(result, ensure_ascii=False)})
                                msgs.append({"role": "system", "content": "上述内部工具已执行，请用自然语言继续回复用户，不要再显示工具调用或 JSON。"})
                                # 输出追加的工具步骤 markdown（仅新增那一步）
                                step_md = format_tool_markdown(executed_steps[-1:])
                                for line in step_md.split('\n'):
                                    if not line:
                                        continue
                                    payload_md = {"choices":[{"delta":{"content": line + "\n"}}]}
                                    yield f"data: {json.dumps(payload_md, ensure_ascii=False)}\n".encode("utf-8")
                                # 启动第二阶段流
                                stream2 = await run_stream(msgs)
                                async for chunk2 in stream2:  # type: ignore
                                    if await request.is_disconnected():
                                        break
                                    try:
                                        ch2 = chunk2.choices[0]
                                        p2 = None
                                        d2 = getattr(ch2, "delta", None)
                                        if d2 and getattr(d2, "content", None):
                                            p2 = d2.content
                                        elif getattr(ch2, "message", None) and getattr(ch2.message, "content", None):
                                            p2 = ch2.message.content
                                        if p2:
                                            payload2 = {"choices":[{"delta":{"content": p2}}]}
                                            yield f"data: {json.dumps(payload2, ensure_ascii=False)}\n".encode("utf-8")
                                    except Exception as ie2:
                                        logger.warning("second stream parse error: %s", ie2)
                                        continue
                                # 内联工具拦截路径：也需要在末尾输出海马体日志（不与上面主路径重复）
                                summary_tail_inline = await finalize_and_build_hippocampus_summary()
                                payload_tail_inline = {"choices":[{"delta":{"content": summary_tail_inline}}]}
                                yield f"data: {json.dumps(payload_tail_inline, ensure_ascii=False)}\n".encode('utf-8')
                                yield b"data: [DONE]\n"
                                return
                        # 正常写出当前块
                        payload = {"choices": [{"delta": {"content": piece}}]}
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n".encode("utf-8")
                        got_any = True
                        assistant_output_buf += piece
                except Exception as inner_e:
                    logger.warning("stream chunk parse error: %s", inner_e)
                    continue
            if not got_any:
                logger.warning("openai stream returned no content pieces (empty response)")
            # 主回答结束后：将本轮 assistant 输出写入 raw_history（形成完整双向历史）
            try:
                if assistant_output_buf.strip():
                    cfg_all_local = load_config()
                    # 不再做 max_raw 截断，确保完整原始对话
                    sm_local = _load_short_mem()
                    sm_local.setdefault("raw_history", [])
                    sm_local['raw_history'].append({"role": "assistant", "content": assistant_output_buf.strip(), "ts": time.time()})
                    _save_short_mem(sm_local)
            except Exception as hist_e:
                logger.warning("append assistant history failed: %s", hist_e)
            # 在主回答结束后输出海马体记忆更新（无论是否写入长期记忆，只要模块启用）
            # 总是输出海马体段落（即使未启用/失败），以便前端可感知
            # 若海马体任务仍在进行，这里等待（主回答已结束，不影响用户主体体验）
            if 'hippocampus_task' in locals() and hippocampus_task is not None:
                if not hippocampus_task.done():
                    try:
                        hippocampus_report = await asyncio.wait_for(hippocampus_task, timeout=6.0)
                    except asyncio.TimeoutError:
                        hippocampus_report = {"enabled": False, "error": "hippocampus timeout"}
                    except Exception as he:
                        hippocampus_report = {"enabled": False, "error": str(he)}
                else:
                    try:
                        hippocampus_report = hippocampus_task.result()
                    except Exception as he2:
                        hippocampus_report = {"enabled": False, "error": str(he2)}
            # 统一输出海马体总结
            summary_tail_main = await finalize_and_build_hippocampus_summary()
            payload_tail_main = {"choices":[{"delta":{"content": summary_tail_main}}]}
            yield f"data: {json.dumps(payload_tail_main, ensure_ascii=False)}\n".encode('utf-8')
            yield b"data: [DONE]\n"
        except Exception as e:
            logger.error("openai streaming error: %s", e)
            err_payload = {"error": str(e), "choices": [{"delta": {"content": f"[ERROR] {str(e)}"}}]}
            yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n".encode("utf-8")
            summary_tail_err = await finalize_and_build_hippocampus_summary()
            payload_tail_err = {"choices":[{"delta":{"content": summary_tail_err}}]}
            yield f"data: {json.dumps(payload_tail_err, ensure_ascii=False)}\n".encode('utf-8')
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
    ids: List[str] = []
    try:
        coll = get_chroma()
        # embed and add
        texts = req.chunks
        emb = asyncio.get_event_loop().run_until_complete(embed_texts(texts)) if asyncio.get_event_loop().is_running() is False else []
        # 若在 event loop 内调用（例如 future 扩展），简单先用零向量避免阻塞
        if not emb:
            dim = get_embedding_dimension()
            emb = [[0.0]*dim for _ in texts]
        gen_ids = [str(uuid.uuid4()) for _ in texts]
        coll.add(ids=gen_ids, documents=texts, embeddings=emb)
        ids = gen_ids
    except Exception as e:
        logger.error("memory write failed: %s", e)
    return WriteResponse(written=len(req.chunks), ids=ids)


@app.get("/memory/debug/all", summary="List synthetic in-memory store (empty)")
def debug_all():
    # No real store yet
    return {"data": [], "note": "No real store implemented yet"}


# Convenience root
@app.get("/")
def root():
    return {"service": "limbic-memory-temp", "endpoints": ["/healthz", "/memory/activate", "/memory/write"], "note": "Temporary backend stub"}


# ---- Admin: reload config & cached clients ----
@app.post("/admin/reload")
def admin_reload():
    """清除 LRU 缓存, 使 config.json 最新内容（包括 system_prompt / api_key 等）立即生效。

    前端或运维在修改 backend/config.json 后可调用本端点，无需整体重启进程。
    """
    try:
        load_config.cache_clear()
        get_chat_client.cache_clear()
        get_embedding_client.cache_clear()
        get_embedding_dimension.cache_clear()
        get_chroma.cache_clear()
        # 访问一次以验证重新加载是否成功
        _ = load_config()
        return {"status": "ok", "reloaded": True}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
