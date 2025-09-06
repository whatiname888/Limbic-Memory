# HTTP 后端对接文档（AIQ 前端适配）

版本: v1.0  
目标读者: 后端工程师  
范围: 仅覆盖 HTTP（含流式）模式，不涉及 WebSocket。

---
## 1. 前端整体调用链
用户输入 → 前端组件 `ChatInput` 调用 `handleSend` → 构造 `chatBody` → 调用 **Next.js 内部 API** `POST /api/chat` (`pages/api/chat.ts`) → 该内部 API 根据 `chatBody.chatCompletionURL` 转发给你的真实后端（下面称“业务后端”）→ 将后端响应（流式或一次性）转换为前端可消费的文本流 → 前端 `Chat.tsx` 读取流并实时渲染。

因此：你只需实现一个对外可访问的 HTTP 接口（URL 写入前端 sessionStorage 或 .env），遵守本文格式即可，无需改动前端源码。

---
## 2. 前端配置来源
`.env` 示例：
```
NEXT_PUBLIC_HTTP_CHAT_COMPLETION_URL=http://127.0.0.1:8002/chat/stream
```
运行时前端会把这个值放入 `chatCompletionURL` 字段发送到 `POST /api/chat`。你可以：
- 提供流式接口：URL 包含 `stream`（前端按“流”处理）
- 提供普通接口：URL 不含 `stream`（前端按“非流”处理）
- 若 URL 包含 `generate`，前端代理会切换到“简化输入模式”（只发最后一条用户消息，字段为 `input_message`）。

---
## 3. 前端发给 `/api/chat` 的请求结构
`Content-Type: application/json`
```json
{
  "messages": [
    { "role": "user", "content": "你好" },
    { "role": "assistant", "content": "(历史AI回复，可选)" },
    { "role": "user", "content": "继续解释RAG" }
  ],
  "chatCompletionURL": "http://127.0.0.1:8002/chat/stream",
  "additionalProps": {
    "enableIntermediateSteps": true
  }
}
```
请求头还会带：
```
Conversation-Id: <会话UUID>
```
> 后端可选用该 ID 做会话隔离或日志标识。

### 3.1 代理层转发给业务后端时的两种模式
| 模式判定 | 业务后端收到的 Body | 说明 |
|----------|--------------------|------|
| URL 含 `generate` | `{ "input_message": "<最后一条user消息文本>" }` | 极简输入模式 |
| 其他（含 `stream` 或普通） | 见下方 OpenAI 风格结构 | 通用聊天模式 |

通用聊天模式 Body（字段值多数可忽略，前端只是占位）：
```json
{
  "messages": [ {"role":"user","content":"..."}, {"role":"assistant","content":"..."} ],
  "model": "string",
  "temperature": 0,
  "max_tokens": 0,
  "top_p": 0,
  "use_knowledge_base": true,
  "top_k": 0,
  "collection_name": "string",
  "stop": true,
  "additionalProp1": {}
}
```
> 你可以只严格解析 `messages`，其他字段忽略。

---
## 4. 业务后端响应协议
分“流式”和“非流式”两类。**建议优先实现流式**，体验更好。

### 4.1 流式（URL 包含 `stream`）
业务后端需返回标准 **SSE 风格文本流**（`Content-Type: text/event-stream` 不是硬性要求，代理不强校验，但建议设置）。“代理层”逐行解析：
- 行以 `data: ` 开头：后跟 JSON，取其中 token 内容
- 行以 `data: [DONE]` 结束整个流
- 行以 `intermediate_data: ` 开头：被视作“中间步骤”结构，转封装为 `<intermediatestep>{JSON}</intermediatestep>` 注入到输出流中

#### 4.1.1 主回答行格式（至少二选一字段）
```jsonc
// 作为 data: 后的 JSON
{
  "choices": [
    { "delta": { "content": "部分文本" } }
  ]
}
```
或：
```jsonc
{
  "choices": [
    { "message": { "content": "完整或增量文本" } }
  ]
}
```
> 前端优先取 `choices[0].message.content`，否则取 `choices[0].delta.content`。

#### 4.1.2 中间步骤行格式（被前端折叠为步骤树）
业务后端输出行：
```
intermediate_data: {"id":"step-1","name":"检索","payload":"检索到3条文档","status":"complete","parent_id":"root-1","intermediate_parent_id":"","time_stamp":"2025-09-06T10:00:00Z"}
```
支持字段：
| 字段 | 必填 | 说明 |
|------|------|------|
| id | 是 | 该步骤唯一标识（用于覆盖/层级引用） |
| name | 是 | 步骤名称（显示在前端） |
| payload | 是 | 文本/Markdown 内容（前端会做简单处理） |
| status | 否 | `in_progress` / `complete`，用于前端覆盖逻辑 |
| parent_id | 否 | 若存在，前端将其插入父步骤的 `intermediate_steps` 内 |
| intermediate_parent_id | 否 | 目前前端未直接使用，可保留为空或同 parent_id |
| time_stamp | 否 | 时间戳，仅记录 |
| error | 否 | 错误信息（可选） |

> **覆盖逻辑**：同 `id` 且 `content.name` 相同，且前端开启“override”时，会替换旧步骤；否则追加。

#### 4.1.3 完整流示例
```
HTTP/1.1 200 OK
Content-Type: text/event-stream

data: {"choices":[{"delta":{"content":"RAG"}}]}
intermediate_data: {"id":"p1","name":"计划","payload":"生成检索计划","status":"in_progress"}
intermediate_data: {"id":"p1","name":"计划","payload":"命中3条候选","status":"complete"}
data: {"choices":[{"delta":{"content":" 是一种 "}}]}
intermediate_data: {"id":"r1","name":"检索","payload":"向量库耗时120ms","status":"complete","parent_id":"p1"}
data: {"choices":[{"delta":{"content":"先检索再生成的范式。"}}]}
data: [DONE]
```
前端最终拼成：`RAG 是一种先检索再生成的范式。`，并显示计划/检索步骤树。

### 4.2 非流式（URL 不含 `stream`）
业务后端直接返回一次性 Body，代理从以下字段优先级依次尝试提取正文：
1. `output`
2. `answer`
3. `value`
4. `choices[0].message.content`
5. 整个解析后的 JSON（再转字符串）
6. 原始文本

返回示例：
```json
{ "answer": "这是完整答案" }
```
或：
```json
{ "choices": [ { "message": { "content": "完整答案" } } ] }
```

### 4.3 错误处理建议
| 场景 | 业务后端做法 | 前端体验 |
|------|--------------|----------|
| 参数错误 | 返回 400 + JSON `{error:"reason"}` | 代理检测非 2xx，将文本包进 `<details>` 返回，前端展示“Something went wrong...” |
| 内部异常 | 返回 500 + 说明文本 | 同上 |
| 流中断 | 提前发送 `data: [DONE]` 或直接断连接 | 前端停止追加 |

> 如果你想自定义友好错误：仍返回 200，但正文为结构化 JSON（带 `answer` 字段说明）。

---
## 5. 前端消费逻辑简述（便于后端理解）
1. **第一次 user 消息**：加入本地会话数组。
2. **调用 `/api/chat`**：进入 `fetch` 流读取 loop：
   - 解码 chunk → 合并残留 partial → 正则抓取 `<intermediatestep>...</intermediatestep>` → JSON.parse → `processIntermediateMessage` 构造树。
   - 剩余纯文本累积到当前正在生成的最后一条 `assistant` 消息 `content`。
3. **完成条件**：读取结束 (`done=true`) → 200ms 后取消 `messageIsStreaming`。

> 你无需关心 UI 逻辑（滚动/再生），只需稳定流格式。

---
## 6. 最小可行实现清单（后端必须支持）
| 级别 | 要素 | 说明 |
|------|------|------|
| 必需 | 支持 POST | 解析上面两种请求体模式 |
| 必需 | 流式 data: 行 | 输出增量文本（如切片），最后 `[DONE]` |
| 推荐 | intermediate_data 行 | 提升可观测性（工具/检索/调用链） |
| 可选 | 非流式模式 | 备用或调试 |
| 可选 | 错误 4xx/5xx | 前端已有兜底 |

---
## 7. 参考伪代码（流式后端示意）
```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import json, asyncio, time

app = FastAPI()

@app.post('/chat/stream')
async def chat_stream(req: Request):
    body = await req.json()
    messages = body.get('messages', [])
    user_last = next((m for m in reversed(messages) if m.get('role')=='user'), {})
    question = user_last.get('content','')

    async def gen():
        yield f"intermediate_data: {json.dumps({'id':'plan','name':'计划','payload':'分析问题','status':'in_progress'})}\n"
        await asyncio.sleep(0.1)
        partial = ''
        answer = f"针对: {question} 的回答示例。"
        for ch in answer:
            yield f"data: {json.dumps({'choices':[{'delta':{'content': ch}}]})}\n"
            await asyncio.sleep(0.02)
        yield f"intermediate_data: {json.dumps({'id':'plan','name':'计划','payload':'完成','status':'complete'})}\n"
        yield "data: [DONE]\n"
    return StreamingResponse(gen(), media_type='text/event-stream')
```

---
## 8. 测试建议
| 用例 | 期望 |
|------|------|
| 普通问题 | 流正确拼接，结束有 `[DONE]` |
| 中间步骤覆盖 | 同 id & name 再发一次，前端只展示最新 payload |
| 错误返回 500 | 前端出现“Something went wrong”并附 details |
| 大文本（>50KB） | 多 chunk 正常拼接，无标签断裂 |
| 不含 stream | 一次性返回被正确显示 |

---
## 9. 注意事项
- 不要让 `<intermediatestep>` 标签跨 chunk（代理端已做一层缓冲，但仍建议完整输出）。
- `intermediate_data:` 行务必是单行 JSON（不要尾随多余空格/多行）。
- 若不打算做中间步骤，可完全不输出任何 `intermediate_data:` 行。
- `[DONE]` 必须单独一行：`data: [DONE]`。
- 返回编码 UTF-8。

---
## 10. 快速对照（最小实现所需字段）
| 场景 | 你需要输出 | 示例 |
|------|------------|------|
| 增量正文 | `data: {"choices":[{"delta":{"content":"字"}}]}` | 多次 |
| 结束 | `data: [DONE]` | 单次 |
| 中间步骤(可选) | `intermediate_data: {..}` | 可多次 |

---
## 11. FAQ
**Q: 可以直接返回纯文本不带 data: 吗？**  可以，但前端当前解析逻辑针对 SSE 分行，更推荐符合格式。  
**Q: 中间步骤必须有 parent_id 吗？**  否，留空则为根步骤；传入已有步骤 id 则形成树结构。  
**Q: 覆盖逻辑如何触发？**  相同 `id` + `name`（且前端开启 override，默认开启）。  
**Q: 不想实现 generate 模式？**  可以，只用 stream URL；前端仍按通用聊天 Body 调用。

---
## 12. 验收 Checklist
- [ ] 流式输出逐行 `data:` 正常
- [ ] 尾部含 `data: [DONE]`
- [ ] 可选中间步骤被前端展示
- [ ] 错误时 4xx/5xx 返回可见 details
- [ ] 长文本不卡顿、不丢字符

---
如需升级到 WebSocket，再补交互/多类型消息即可；当前无需考虑。
