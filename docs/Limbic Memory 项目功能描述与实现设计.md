

# Limbic Memory 项目功能描述与实现设计

恭喜你们团队参加 NVIDIA x 阿里云 2025 智能体 Hackathon！基于仓库 https://github.com/whatiname888/Limbic-Memory 和初始代码 https://github.com/HeKun-NVIDIA/hackathon_aiqtoolkit，本文档详细描述项目需要实现的功能（基于之前的“海马忆存器”构想），并设计如何整合到 Hackathon 框架中。设计重点：前端沿用初始 UI（NeMo-Agent-Toolkit-UI），后端使用 NeMo Agent Toolkit（AIQ）内置工具（如 Mem0、LlamaIndex、LangChain）+ 自定义 Tool 实现核心模块。整体作为外挂参考架构，强调生物启发（海马体模拟）、动态记忆（激活/忘记/时间感）和 MCP-like 主动控制。

项目定位：开源蓝图+基础 demo，不求生产级，但为 Hackathon 提供可演示智能体（如增强对话代理）。团队 4 人分工建议：1 人前端（UI 沿用+小改）、1 人后端工具开发（自定义 Tool）、1 人集成/测试（Workflow 组装）、1 人文档/优化（README、伪代码）。

## 需要实现的功能详细描述
基于项目核心（生物启发动态记忆），功能分解为 4 个模块+整体系统。每个包括目标、输入/输出、关键特性。优先级：先实现核心（存储/激活），再加高级（如 MCP）。

### 1. **激活模块** - 模拟回忆激活
   - **目标**：根据输入（对话/查询）动态激活相关记忆，像人类“突然想起”，注入 LLM 上下文。
   - **输入**：当前查询字符串、阈值（e.g., 相似度 0.7）。
   - **输出**：激活的记忆列表（Top-K 结果），注入为提示。
   - **关键特性**：向量检索+噪声（概率阈值模拟不确定性）；支持时间过滤（e.g., 只激活最近记忆）。
   - **Hackathon 相关**：用于增强代理响应，避免 LLM 遗忘历史。

### 2. **记忆存储模块** - 带忘记机制
   - **目标**：存储新记忆，但渐进忘记不重要细节，实现“选择性遗忘”。
   - **输入**：内容字符串、初始分数（e.g., 相关度 1.0）、时间戳。
   - **输出**：存储 ID 或确认；定期忘记低分记忆。
   - **关键特性**：分层存储（短期缓存如 Redis、长期如 SQLite/Vector DB）；衰减机制（时间/相关度<阈值删除）；异步不阻塞对话。
   - **Hackathon 相关**：整合 Mem0（内置长期记忆）或 Redis（高速缓存），为代理提供持久化。

### 3. **海马体模块** - 短期到长期转换
   - **目标**：模拟海马体，将短期上下文转为长期记忆，添加顺序感。
   - **输入**：短期上下文列表、序列 ID。
   - **输出**：转换确认；更新存储模块。
   - **关键特性**：阈值触发（e.g., 上下文>5 条时转换）；添加时间戳/事件顺序 ID；嵌入生成（向量表示）。
   - **Hackathon 相关**：用 LlamaIndex（内置数据索引）处理转换，提升代理的“学习”能力。

### 4. **主动回忆模块** - MCP 控制权
   - **目标**：让 LLM 主动控制记忆（回忆/存储/删除），通过结构化指令。
   - **输入**：LLM 输出（含 MCP-like XML 标签，如 `<mcp:recall depth="deep" time="recent">keyword</mcp>`）。
   - **输出**：解析结果，注入激活的记忆。
   - **关键特性**：解析器（正则/XML）；集成激活/存储模块；支持深度（shallow/deep）和时长（recent/all）。
   - **Hackathon 相关**：扩展 Agent（如 react_agent），让代理主动调用记忆 Tool。

### 5. **整体系统特性**
   - **目标**：外挂式、实时（<1s）、兼容多种 LLM（如 Qwen via Bailian API）。
   - **输入/输出**：端到端对话增强（输入查询→输出带记忆响应）。
   - **关键特性**：异步操作；MCP 协议整合；生物启发（如噪声忘记）；监控（如 OpenTelemetry 追踪性能）。
   - **Hackathon 相关**：用 Workflow 组装所有模块；测试用 NeMo test Tool；可视化用 Arize Phoenix。

## 如何设计去实现
设计基于 NeMo Agent Toolkit：后端用 YAML 配置 Workflow（react_agent 类型），内置 Tool（如 Mem0、LlamaIndex）+ 自定义 Tool 实现模块。前端沿用初始 UI（npm run dev），加按钮/输入支持 MCP 指令。依赖 Python 3.8+、NeMo（pip install）、LangChain 等。仓库结构：`src/` 放 Tool 代码、`configs/` 放 YAML、`demo/` 放 Notebook 测试。

### 1. **整体架构设计**
   - **后端 Workflow**：用 `hackathon_config.yml` 扩展，添加自定义 Tool 到 `functions` 和 `tool_names`。示例：
     ```yaml
     functions:
       limbic_activate:  # 自定义激活 Tool
         _type: custom_limbic_activate  # 见自定义 Tool
         description: "Activate memories based on query"
       limbic_store:  # 存储 Tool
         _type: custom_limbic_store
         description: "Store memory with decay"
       # ... 其他模块类似
     
     workflow:
       _type: react_agent
       tool_names:
         - limbic_activate
         - limbic_store
         - internet_search  # 内置继承
       llm_name: default_llm  # 用 Qwen
     ```
   - **前端集成**：沿用初始 UI（external/aiqtoolkit-opensource-ui），加自定义组件（如记忆查看面板）。启动：`aiq serve --config_file configs/hackathon_config.yml`。
   - **数据流**：用户输入 → Agent 规划（react_agent）→ 调用 Tool（e.g., 激活→存储）→ LLM 生成响应 → UI 显示。

### 2. **模块实现步骤**
   - **准备**：克隆初始代码，安装（npm install 前端；pip install 后端）。在 `NeMo-Agent-Toolkit/packages/nat/plugins/langchain/src/nat/tools/` 加自定义 Tool 文件（参考 tavily_internet_search.py）。
   - **自定义 Tool 实现**（用 @tool 装饰器注册）：
     - **激活模块**：用 LangChain + FAISS 向量搜索。集成 Mem0 查询历史。
       ```python
       from langchain.tools import tool
       import faiss  # 或 LlamaIndex
       
       @tool
       def limbic_activate(query: str, threshold: float = 0.7) -> list:
           # 向量嵌入 + 搜索（添加噪声）
           # 返回激活记忆
           pass
       ```
     - **存储模块**：用 Mem0 存储 + 定时任务衰减（e.g., score -= 0.1/day）。
       ```python
       @tool
       def limbic_store(content: str, score: float = 1.0) -> str:
           # 用 Mem0/Redis 存储 + 时间戳
           # 异步忘记 low-score
           pass
       ```
     - **海马体模块**：用 LlamaIndex 索引转换。
       ```python
       @tool
       def limbic_hippocampus(short_context: list, seq_id: int) -> str:
           # 阈值检查 + 嵌入 + 调用存储 Tool
           pass
       ```
     - **主动回忆模块**：解析 LLM 输出，调用其他 Tool。
       ```python
       @tool
       def limbic_recall(llm_output: str) -> str:
           # 正则解析 MCP + 调用激活
           pass
       ```
   - **集成 Workflow**：在 YAML 中组装。测试：用 NeMo test Tool 运行单元测试。
   - **监控/优化**：用 OpenTelemetry + Arize Phoenix 追踪 Tool 调用；Weights & Biases 记录实验。

### 3. **团队分工与时间线建议**
   - **人1 (前端)**：沿用 UI，加记忆显示/ MCP 输入（1-2 天）。
   - **人2 (后端开发)**：实现自定义 Tool（2-3 天）。
   - **人3 (集成/测试)**：组装 Workflow、端到端测试（2 天）；用 RagaAI 检查幻觉。
   - **人4 (文档/优化)**：更新 README、伪代码、Hackathon 演示脚本（全程）。
   - **总时间**：1 周内 demo 跑通。优先 MVP：存储+激活。

### 4. **潜在挑战与Tips**
   - **兼容**：确保自定义 Tool 兼容 NeMo（参考文档）。Windows 环境变量如 TAVILY_API_KEY。
   - **性能**：用 Redis 缓存加速；异步（如 Celery）避免阻塞。
   - **Hackathon 亮点**：强调生物启发+ Mem0 整合，演示场景如“长期对话代理记住用户偏好”。
   - **扩展**：后期加多模态（Agnostic）或数据库（MySQL）。

这个设计作为蓝图，仓库可放本文件到 `docs/`。如果需要具体代码 snippet 或调试帮助，分享更多细节！加油，期待你们 Hackathon 拿奖！😄

