## openclaw vs nanobot：全面简化对比分析

### 一、整体规模对比

| 维度 | openclaw | nanobot | 简化倍数 |
|------|----------|---------|---------|
| **语言** | TypeScript (Node.js) | Python (asyncio) | — |
| **核心源码文件数**（非测试） | ~2910 个 .ts 文件 | ~64 个 .py 文件 | **~45x** |
| **src 一级模块数** | 52 个目录 | 10 个目录 | **~5x** |
| **agents/agent 核心行数** | 仅 agent-runner 系列就 2261 行 | loop.py 580 行 | **~4x**（仅对等部分）|
| **system prompt 构建** | 728 行 (system-prompt.ts) | 198 行 (context.py) | **~3.7x** |
| **记忆压缩** | 464 行 (compaction.ts) | 431 行 (memory.py，含全部记忆逻辑) | **~1:1**（nanobot 保留了核心）|
| **技能数量** | 52 个 | 8 个 | **~6.5x** |
| **扩展（extensions）** | 43 个 | 0 个 | **完全砍掉** |
| **渠道（channels）** | ~110 个文件 + 独立 Discord/Telegram/Slack/Signal 等模块 | ~15 个文件（全在 channels 目录） | **~7x** |

---

### 二、逐模块对比：砍了什么、简化了什么、保留了什么

#### 1. 🔴 完全砍掉的模块

这些模块在 openclaw 中存在，但在 nanobot 中**完全没有**：

| openclaw 模块 | 功能 | 砍掉原因 |
|--------------|------|---------|
| **apps/** (android/ios/macos) | 原生移动端客户端 | nanobot 定位服务端 Agent，不需要客户端 |
| **ui/** | Web 前端界面（Vite + React） | nanobot 纯后端，通过 IM 渠道交互 |
| **extensions/** (43 个) | 插件系统：Discord、Feishu、LanceDB 记忆、Ollama、VLLM 等 | nanobot 把渠道内置化，不需要插件架构 |
| **packages/** (clawdbot/moltbot) | 独立子包 | monorepo 拆分，nanobot 单包 |
| **gateway/** (219 文件) | HTTP API 网关、OpenResponses 兼容、REST API、认证、Tailscale | nanobot 没有 HTTP API 层，直接 CLI/IM |
| **browser/** (95 文件) | 内置浏览器控制（CDP/Playwright） | nanobot 不内置浏览器，依赖 shell 工具 |
| **plugin-sdk/** (86 文件) | 第三方插件开发 SDK | nanobot 用 skills（Markdown）替代 |
| **plugins/** (54 文件) | 插件运行时加载 | 同上 |
| **tui/** | 终端 UI 界面（TUI 渲染） | nanobot 用简单 CLI |
| **wizard/** | 交互式引导安装向导 | nanobot 用配置文件 |
| **acp/** | Agent Communication Protocol | nanobot 用简单的子 Agent spawn |
| **pairing/** | 设备配对（手机和 PC） | nanobot 不支持 |
| **media-understanding/** | 图片/视频/音频理解 | nanobot 简单处理 |
| **tts/** | 文字转语音 | nanobot 不支持 |
| **link-understanding/** | URL 理解和预览 | nanobot 通过 web_fetch 工具实现 |
| **canvas-host/** | Canvas 画布展示 | nanobot 不支持 |
| **secrets/** (33 文件) | 密钥管理系统 | nanobot 用环境变量 |
| **security/** (19 文件) | 审计、安全扫描、危险工具检测 | nanobot 简单权限 + 沙箱 |
| **logging/** (16 文件) | 子系统级别结构化日志 | nanobot 用 loguru 一行搞定 |
| **infra/** (229 文件) | 底层基础设施（备份、设备发现、Bonjour、剪贴板等） | nanobot 大幅简化 |
| **i18n/** | 国际化 | nanobot 不支持 |

#### 2. 🟡 大幅简化的模块

| 模块 | openclaw 规模 | nanobot 规模 | 简化方式 |
|------|-------------|-------------|---------|
| **Agent Loop** | agent-runner 系列 5 个文件 ~2261 行 + auto-reply 模块 ~194 个文件 | loop.py 1 个文件 580 行 | ReAct 循环核心不变，砍掉了：消息队列调度、指令解析（directive handling）、流式分块、回复路由、多后端 dispatch、elevated 权限审批流、ACP 集成 |
| **System Prompt** | system-prompt.ts 728 行 + bootstrap-budget.ts 349 行 + bootstrap-files.ts 118 行 + bootstrap-hooks.ts + workspace.ts 641 行 | context.py 198 行 | 砍掉了：Sandbox 提示、Reply Tags、Messaging 路由指令、Voice/TTS 提示、Model Aliases、Safety 宪法条款、Canvas/Nodes 工具说明、Owner 身份哈希、Bootstrap 预算控制、Reasoning 标签格式 |
| **记忆系统** | memory/ 63 个文件（向量嵌入、SQLite-vec、LanceDB、Voyage batch、Gemini batch、语义搜索、MMR 排序） | memory.py 1 个文件 431 行 | 从**向量数据库语义搜索** → **纯文本 Markdown 文件**。砍掉了全部 embedding 相关代码（大概 50+ 文件）。保留了 LLM 做摘要压缩的核心逻辑 |
| **Compaction（压缩）** | compaction.ts 464 行 + session-transcript-repair.ts + 多步分块摘要 + tool_use/tool_result 配对修复 | memory.py 中 ~200 行 | 核心的分块摘要保留，砍掉了：token 精确估算（用字符/4 近似）、oversized message 降级策略、tool_use 孤儿修复、多阶段合并摘要 |
| **工具系统** | agents/tools/ ~80+ 文件（browser-tool、canvas-tool、pdf-tool、image-tool、sessions-* 系列、discord-actions、telegram-actions、web-guarded-fetch 等） | agent/tools/ 10 个文件 | 只保留了 6 类核心工具：filesystem、shell、web、message、cron、spawn。砍掉了：browser、canvas、pdf、image、sessions 管理、channel-specific actions、gateway 工具 |
| **子 Agent** | subagent-spawn.ts 785 行 + subagent-registry.ts + subagent-attachments.ts + ACP spawn 24762 行 + ACP stream 10996 行 | subagent.py 275 行 | 保留最简子 Agent spawn，砍掉了：ACP 协议（Agent Communication Protocol）、harness runtime、sub-agent 注册表、附件传递、stream forwarding |
| **Provider** | agents/ 里有 ~30+ 个 model-* 文件（model-catalog、model-selection、model-compat、model-auth、venice-models、doubao-models、huggingface-models 等）+ auth-profiles 系列 + API key 轮换 | providers/ 8 个文件 | 从支持 30+ 模型厂商的精细适配 → 用 LiteLLM + OpenAI SDK 统一代理。砍掉了：厂商特定模型目录、auth profile 管理、API key 轮换、context window 自动发现 |
| **渠道** | channels/ 110 文件 + 独立的 discord/(99 文件)、telegram/(74 文件)、slack/(73 文件)、signal/(19 文件)、imessage/(20 文件)、line/(30 文件)、whatsapp/(2 文件) 等模块 | channels/ 15 个文件 | 渠道数量从 15+ 减到 12 个，每个渠道从多文件模块 → 单文件实现。砍掉了：message debounce、typing lifecycle、thread binding、group activation、conversation label、inline buttons、status reactions 等精细交互 |
| **配置系统** | config/ 137 文件（schema 验证、环境变量替换、legacy 迁移 3 个阶段、backup rotation、includes scan 等） | config/ 4 个文件 | 砍掉了：schema 精细验证、配置 include 机制、legacy 迁移、配置备份轮换。用 YAML 简单加载替代 |
| **Cron** | cron/ 44 文件 | cron/ 3 个文件 | 核心调度逻辑保留，砍掉了 cron 的 UI 展示和精细管理 |
| **CLI** | cli/ 187 文件 + commands/ 235 文件 | cli/ 2 个文件 | 从丰富的子命令系统（status、config validate、gateway start/stop、session export 等）→ 极简 CLI（start、init 等） |
| **Session** | sessions/ 9 文件 + 各处分散的 session 管理逻辑 | session/ 2 个文件 | 保留 JSONL 持久化核心，砍掉了 session fork、session store、跨 session 通信路由 |

#### 3. 🟢 基本保留的核心设计

这些是 nanobot 保留的 openclaw "精华"：

| 核心设计 | 保留程度 | 说明 |
|---------|---------|------|
| **ReAct 循环** | ✅ 完整保留 | LLM → 工具调用 → 结果注入 → 继续推理 |
| **分层 System Prompt** | ✅ 保留架构 | Identity → Bootstrap → Memory → Skills，只是每层内容更精简 |
| **Bootstrap Files** | ✅ 完整保留 | AGENTS.md / SOUL.md / USER.md / TOOLS.md |
| **Skills 系统** | ✅ 保留核心 | 摘要 + 按需加载的架构完全一致 |
| **LLM 记忆压缩** | ✅ 保留核心 | 旧消息摘要归档、offset 管理 |
| **子 Agent 架构** | ✅ 保留核心 | Spawn → 独立运行 → 回报结果 |
| **多渠道抽象** | ✅ 保留架构 | BaseChannel → 具体渠道子类 |
| **Heartbeat** | ✅ 保留核心 | 定时心跳唤醒 Agent |
| **工具注册表模式** | ✅ 保留核心 | 统一注册 + 按需过滤 |
| **Cron 调度** | ✅ 保留核心 | APScheduler 实现 |
| **Session JSONL 持久化** | ✅ 保留核心 | 消息逐行追加 |

---

### 三、架构层面最大的 5 个简化

#### 简化 1：去掉了整个 Gateway 层

```
openclaw:  用户 → HTTP API/WebSocket (Gateway) → Agent Runner → LLM
nanobot:   用户 → IM 消息 → Channel → Agent Loop → LLM
```

openclaw 有一个完整的 HTTP 网关服务器（219 文件），支持 REST API、OpenResponses 协议兼容、Tailscale 认证、RBAC 角色策略等。nanobot 直接从 IM 渠道进入 Agent Loop，**没有 HTTP API 层**。

#### 简化 2：向量记忆 → 纯文本记忆

```
openclaw:  MEMORY.md + SQLite-vec/LanceDB + Embedding (Voyage/Gemini/OpenAI/Mistral)
           → 语义搜索 + MMR 排序 + 时间衰减 + Hybrid 检索
nanobot:   MEMORY.md (纯 Markdown 文件)
           → LLM 直接读取 + LLM 做摘要压缩
```

openclaw 的记忆模块有 63 个文件，支持 5 种 embedding 提供商、向量数据库存储、MMR 多样性排序、hybrid 混合检索。nanobot 把这一切砍掉，回到最原始的**纯文本文件 + LLM 理解**。

#### 简化 3：去掉了插件/扩展架构

```
openclaw:  Plugin SDK → Extension Runtime → 43 个 Extensions → Dynamic Loading
nanobot:   Skills (Markdown 文件) → 静态加载
```

openclaw 有完整的 Plugin SDK（86 文件）、运行时加载器（54 文件）、43 个扩展。nanobot 用 **Markdown 文件 + read_file** 替代整个插件系统——技能就是一个 SKILL.md 文件，够了。

#### 简化 4：多厂商模型适配 → LiteLLM 统一代理

```
openclaw:  30+ 个 model-*.ts（每个厂商的模型目录、auth 配置、context window 发现）
           + auth-profiles（API key 轮换、冷却策略）
           + 厂商特定 stream 处理（ollama-stream, openai-ws-stream）
nanobot:   LiteLLM Provider（1 个文件）→ 代理所有厂商
           + OpenAI Provider（1 个文件）
           + Azure OpenAI Provider（1 个文件）
```

#### 简化 5：TypeScript monorepo → Python 单包

```
openclaw:  pnpm workspace + tsconfig + tsdown + vitest (6 种配置)
           + apps/packages/extensions/skills 四层
           + 4887 个 TS 文件
nanobot:   pyproject.toml + 1 个 Python 包
           + 64 个 Python 文件
```

---

### 四、总结：nanobot 的简化哲学

用一句话概括：**nanobot 保留了 openclaw 的骨架（架构模式），砍掉了肌肉（工程细节）**。

```
保留的"骨架"：                     砍掉的"肌肉"：
├─ ReAct 循环                      ├─ HTTP Gateway（219 文件）
├─ 分层 System Prompt              ├─ 向量记忆系统（63 文件）
├─ Bootstrap Files                 ├─ 插件/扩展系统（183 文件）
├─ Skills 摘要+按需加载            ├─ 浏览器控制（95 文件）
├─ LLM 记忆压缩                    ├─ 移动端客户端
├─ 子 Agent                        ├─ Web UI
├─ 多渠道抽象                      ├─ 30+ 厂商模型适配
├─ Heartbeat/Cron                  ├─ 安全审计系统
└─ Session 持久化                  ├─ 国际化、TTS、Canvas
                                    └─ 精细的 error handling / retry / backoff
```

如果你要向面试官解释：

> "我研究了一个生产级 Agentic 项目（2900+ 个源文件、TypeScript），然后用 Python 做了一个精简版（64 个文件），**保留了所有核心架构模式**——ReAct 循环、分层 Prompt、Skills 按需加载、LLM 记忆压缩、子 Agent 编排——**但砍掉了工程复杂度**：去掉了 HTTP 网关层、向量记忆数据库、插件 SDK、浏览器控制、移动客户端。这让我既理解了生产系统的完整设计，又能清楚地解释每个模块为什么存在、什么时候需要扩展。"

---

### 五、被砍掉的模块中，哪些值得深入学习？

上面砍掉的模块不是都不重要，其中有三个是面试**深水区**——面试官一旦追问你就需要能讲清楚：

| 深水区 | openclaw 模块 | 为什么面试会考 | 详见 |
|--------|--------------|---------------|------|
| **向量记忆（RAG）** | `memory/` 63 个文件 | 纯文本记忆有天花板，面试官 100% 会问 Embedding、向量检索、MMR | `07-production-depth.md` 第一章 |
| **安全体系** | `security/` 19 个文件 | Agent 能操作文件和执行命令，安全是生产上线的前提 | `07-production-depth.md` 第二章 |
| **Token 精细化管控** | `bootstrap-budget.ts` + `compaction.ts` | 每个 token 都是钱，面试官会问成本优化 | `07-production-depth.md` 第三章 |

> **学习策略**：先掌握 nanobot 的简化实现（01-06），再去 `07-production-depth.md` 看生产级的完整方案。面试时先讲简化版，再**主动**补充"生产环境中还需要..."，这比被动等追问要强得多。