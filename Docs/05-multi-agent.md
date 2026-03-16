# 05 — 多 Agent 协作与基础设施

> 学习笔记 · 源码位置：`nanobot/agent/subagent.py`、`nanobot/bus/`、`nanobot/channels/`、`nanobot/providers/`

---

## 一、多 Agent 协作

### 为什么需要子 Agent？

有些任务很耗时（比如"帮我搜集最近一周的 AI 新闻并整理成文档"），如果主 Agent 自己干，用户就得等着，期间什么事都做不了。

解决方案：**主 Agent 当"老板"，把活派给"子 Agent"去后台干，自己立刻回来继续接客**。

### 核心模式：Fire-and-Forget

```
用户："帮我整理 AI 新闻"
    ↓
主 Agent → spawn("整理 AI 新闻")
    ↓
立刻回复用户："好的，已安排后台处理，完成后通知你"
    ↓
子 Agent 在后台默默干活（读文件、搜网页、写文件...）
    ↓
干完了 → 通过 MessageBus 注入一条系统消息
    ↓
主 Agent 收到系统消息 → 用自然语言把结果告诉用户
```

### 延伸：多 Agent 协作的四种模式

当前项目用的是 Fire-and-Forget，但业界常见的多 Agent 协作模式有四种，面试经常考：

**模式一：Fire-and-Forget（✅ 当前实现）**

上面已经讲过。主 Agent 派生子 Agent 后**立即返回**，不等结果。优点是不阻塞、响应快；限制是主 Agent 拿不到子 Agent 的返回值做下一步决策。

**模式二：同步等待（Await）**

```
主 Agent ─ spawn() ─→ 子 Agent
    │                      │
    └─ await 等结果 ────────┘
              │
        拿到结果 → 继续 ReAct
```

适用于后续步骤强依赖子 Agent 结果的场景（如先爬数据再分析）。代价是主 Agent 在等待期间无法响应新消息。

**模式三：DAG 编排（Pipeline）**

```
        ┌─ 子 Agent A ─┐
主 Agent ┤              ├→ 子 Agent C → 汇总
        └─ 子 Agent B ─┘
```

多个子 Agent 按**依赖关系**组织成有向无环图。适用于复杂多步骤任务（并行爬取 → 汇总分析 → 生成报告）。代价是实现复杂，需要编排状态管理。

**模式四：事件驱动回调**

```
主 Agent ─ spawn(callback="write_file") ─→ 子 Agent
                                              │
                                         完成后执行回调
                                         （不通知主 Agent）
```

子 Agent 完成后**直接执行回调动作**（写文件、发通知），主 Agent 不参与。适用于完全独立的后台任务。代价是主 Agent 对子 Agent 状态不可见。

| 模式 | 阻塞主 Agent？ | 结果可用？ | 复杂度 | 典型场景 |
|------|:---:|:---:|:---:|------|
| Fire-and-Forget | ❌ | 异步通知 | 低 | 耗时后台任务 |
| 同步等待 | ✅ | 直接拿到 | 低 | 有依赖的串行任务 |
| DAG 编排 | ❌ | 汇总拿到 | 高 | 多步并行流水线 |
| 事件驱动回调 | ❌ | 不可见 | 中 | 独立后台作业 |

---

### 子 Agent 的特点

1. **有独立的工具集**：文件、Shell、网络工具都有，但**没有 message 和 spawn**——防止子 Agent 自己再派活或直接给用户发消息
2. **有独立的 ReAct 循环**：最多 15 轮（比主 Agent 的 40 轮少）
3. **没有记忆系统**：一次性任务，干完就销毁
4. **结果通过消息总线回报**：而不是直接返回给主 Agent

### 结果回报机制

子 Agent 完成后，构造一条特殊的入站消息：

```python
InboundMessage(
    channel="system",                    # 标记为系统内部消息
    sender_id="subagent",
    chat_id="telegram:12345",            # 原始会话的路由信息
    content="[Subagent '整理新闻' completed] ...",
)
```

主 Agent 在 `_process_message` 中识别到 `channel == "system"`，就知道这是子 Agent 的回报，解析出真实的 channel 和 chat_id，触发新一轮 ReAct 循环，把结果自然语言化后回复用户。

### 生命周期管理

```python
_running_tasks: dict[task_id → asyncio.Task]    # 全局：所有在跑的子 Agent
_session_tasks: dict[session_key → {task_ids}]   # 按会话分组
```

- 每个子 Agent 是一个 `asyncio.Task`
- 任务结束时通过 `add_done_callback` 自动清理引用
- 用户发 `/stop` 时，按 session 批量取消所有子 Agent

---

## 二、消息总线（MessageBus）

### 设计思想

消息总线的作用就是一句话：**让渠道和 Agent 互不认识对方**。

渠道不需要知道 Agent 怎么工作，Agent 也不需要知道消息从哪来。双方都只和 Bus 打交道。

### 实现

极其简单——就是两个 asyncio.Queue：

```python
class MessageBus:
    def __init__(self):
        self.inbound = asyncio.Queue()   # 渠道 → Agent
        self.outbound = asyncio.Queue()  # Agent → 渠道
```

| 方法 | 谁调用 | 做什么 |
|------|-------|-------|
| `publish_inbound(msg)` | 渠道 / 子 Agent | 往入站队列放消息 |
| `consume_inbound()` | AgentLoop | 从入站队列取消息 |
| `publish_outbound(msg)` | AgentLoop / MessageTool | 往出站队列放回复 |
| `consume_outbound()` | ChannelManager | 从出站队列取回复 |

### 消息类型

```python
InboundMessage:  谁发的(sender_id) + 哪个群(chat_id) + 说了啥(content) + 附件(media)
OutboundMessage: 发到哪(channel + chat_id) + 内容(content) + 附件(media)
```

`session_key` = `channel:chat_id`，这是会话的唯一标识。

---

## 三、渠道系统（Channels）

### 设计思想

每个聊天平台（Telegram、钉钉、Slack...）的 API 都不一样，但 Agent 不应该关心这些差异。所以用**抽象基类 + 具体实现**的方式统一接口。

### BaseChannel 抽象基类

每个渠道必须实现三个方法：

```python
class BaseChannel(ABC):
    async def start(self)              # 连接平台，开始监听
    async def stop(self)               # 断开连接
    async def send(self, msg)          # 发送消息
```

还有一些通用逻辑：

- **权限检查**：`is_allowed(sender_id)` 检查白名单，空列表 = 拒绝所有，`"*"` = 允许所有
- **消息接收**：`_handle_message()` 检查权限后封装成 InboundMessage 丢进 Bus

### ChannelManager

负责管理所有渠道的启停和消息路由：

```
启动时：
  1. 扫描配置，初始化所有 enabled 的渠道
  2. 启动出站消息分发器（_dispatch_outbound）
  3. 启动所有渠道

出站分发器：
  1. 从 Bus 的出站队列取消息
  2. 根据 msg.channel 找到对应的渠道
  3. 调用 channel.send(msg) 发出去
```

**进度消息过滤**：出站分发器会检查 `_progress` 和 `_tool_hint` 元数据标记，根据配置决定是否发送"思考中..."之类的进度消息。

---

## 四、LLM 抽象层（Provider）

### 设计思想

不同的 LLM 提供商（OpenAI、Anthropic、DeepSeek...）的 API 格式不完全相同。Provider 层的作用是统一接口，让上层代码不用关心具体用的是哪家模型。

### LLMProvider 基类

核心就两个方法：

```python
class LLMProvider(ABC):
    async def chat(self, messages, tools, model, ...)     # 发一次请求
    async def chat_with_retry(self, messages, tools, ...)  # 带重试的版本
```

### 重试策略

`chat_with_retry` 实现了**指数退避重试**：

```
第1次失败 → 等1秒 → 重试
第2次失败 → 等2秒 → 重试
第3次失败 → 等4秒 → 最后一次尝试
```

只有**瞬态错误**才重试（429 限流、500 服务器错误、超时等），非瞬态错误（如 400 参数错误）直接返回。

### 消息清洗

`_sanitize_empty_content` 方法处理各种边界情况：

- 空字符串 content → 替换为 `"(empty)"` 或 `None`
- 空的 text block → 过滤掉
- dict 格式的 content → 包装成 list

这些"脏数据"通常来自 MCP 工具返回空结果，如果不清洗会导致某些 Provider 返回 400 错误。

---

## 五、Prompt 组装（ContextBuilder）

### System Prompt 的结构

ContextBuilder 负责把各种信息拼成 system prompt：

```
1. 核心身份（Identity）
   "You are nanobot, a helpful AI assistant."
   + 运行时信息 + 工作目录 + 使用准则

2. 用户定制文件（Bootstrap Files）
   AGENTS.md、SOUL.md、USER.md、TOOLS.md
   （用户放在工作目录下的自定义指令）

3. 长期记忆（Memory）
   从 MEMORY.md 读取

4. 常驻技能（Always-on Skills）
   标记了 always=true 的 SKILL.md 内容

5. 技能列表（Skills Summary）
   所有可用技能的 XML 摘要，Agent 按需用 read_file 读取
```

### 运行时上下文注入

每条用户消息前面会注入一段运行时信息：

```
[Runtime Context — metadata only, not instructions]
Current Time: 2024-01-01 10:00 (Monday) (CST)
Channel: telegram
Chat ID: 12345
```

这段信息**不会持久化到 session**——因为它每次都会重新生成。

---

## 六、配置系统（Config）

整个系统通过一个 JSON 配置文件驱动，用 Pydantic 做 Schema 校验：

```json
{
    "agents": { "defaults": { "model": "...", "maxTokens": 8192 } },
    "channels": { "telegram": { "enabled": true, "token": "..." } },
    "providers": { "openai": { "apiKey": "..." } },
    "tools": { "exec": { "timeout": 60 }, "mcpServers": { ... } }
}
```

**亮点**：支持 camelCase 和 snake_case 双向兼容——JSON 里写 camelCase，Python 里用 snake_case。

---

## 面试话术

> **多 Agent 协作**："我们采用 Fire-and-Forget 的 Delegation 模式。主 Agent 通过 spawn 工具委派任务给子 Agent，子 Agent 运行在独立的 asyncio.Task 中，完成后通过消息总线异步通知主 Agent。这种设计保证了主 Agent 不会阻塞，用户体验好。"

> **消息总线**："用双 asyncio.Queue 实现的 Producer-Consumer 模式，将渠道和 Agent 完全解耦。任何新渠道只需实现 start/stop/send 三个方法，注册后就能自动参与消息路由。"

> **Provider 层**："LLM 调用抽象为统一接口，通过 chat_with_retry 实现指数退避重试。消息在发送前会经过清洗，处理空内容、格式不兼容等边界情况，保证不会因为脏数据导致 400 错误。"

---

**上一篇 ←** [04-memory.md](./04-memory.md) 记忆系统  
**下一篇 →** [06-interview.md](./06-interview.md) 面试高频问题
