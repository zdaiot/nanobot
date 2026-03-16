# Agent 架构文档

## 概览

nanobot 的 Agent 系统由三个核心层组成：**消息总线（MessageBus）**、**主 Agent 循环（AgentLoop）** 和 **子 Agent 管理器（SubagentManager）**。三者通过异步队列解耦，形成一个可扩展的多 Agent 协作框架。

```
┌─────────────────────────────────────────────────────────┐
│                    外部渠道（Channel）                    │
│          Telegram / Slack / Discord / CLI / ...          │
└────────────────────────┬────────────────────────────────┘
                         │ InboundMessage
                         ▼
┌─────────────────────────────────────────────────────────┐
│                    MessageBus（消息总线）                  │
│         inbound Queue  ←→  outbound Queue               │
└────────────┬────────────────────────┬───────────────────┘
             │ consume_inbound        │ publish_outbound
             ▼                        ▲
┌─────────────────────────────────────────────────────────┐
│                    AgentLoop（主 Agent）                   │
│                                                         │
│  run() → _dispatch() → _process_message()               │
│                ↓                                        │
│         _run_agent_loop()  ← ReAct 循环                  │
│          LLM ↔ ToolRegistry                             │
│                ↓                                        │
│         SpawnTool.execute()                             │
└────────────────────┬────────────────────────────────────┘
                     │ spawn()
                     ▼
┌─────────────────────────────────────────────────────────┐
│               SubagentManager（子 Agent 管理器）           │
│                                                         │
│  _run_subagent() → LLM ↔ 独立 ToolRegistry              │
│                ↓                                        │
│         _announce_result()                              │
│                ↓                                        │
│    bus.publish_inbound(channel="system")  ──────────────┼──→ MessageBus
└─────────────────────────────────────────────────────────┘
```

---

## 核心组件

### 1. MessageBus（`nanobot/bus/queue.py`）

消息总线是整个系统的通信中枢，将外部渠道与 Agent 核心完全解耦。

```
inbound Queue   ← 外部渠道 / 子 Agent 结果注入
outbound Queue  → 外部渠道 / 进度推送
```

| 方法 | 说明 |
|------|------|
| `publish_inbound(msg)` | 向 Agent 投递入站消息（渠道或子 Agent 调用） |
| `consume_inbound()` | Agent 消费入站消息（阻塞直到有消息） |
| `publish_outbound(msg)` | Agent 向渠道推送出站消息 |
| `consume_outbound()` | 渠道消费出站消息 |

**消息类型：**

- `InboundMessage`：入站消息，携带 `channel`、`sender_id`、`chat_id`、`content`、`media`、`metadata`。`session_key` 由 `channel:chat_id` 自动生成，也可通过 `session_key_override` 覆盖（用于线程级会话隔离）。
- `OutboundMessage`：出站消息，携带 `channel`、`chat_id`、`content`、`reply_to`、`media`、`metadata`。

---

### 2. AgentLoop（`nanobot/agent/loop.py`）

主 Agent 的核心处理引擎，负责消息调度、LLM 调用、工具执行和会话管理。

#### 2.1 消息调度流程

```
run()
 └─ consume_inbound()          # 从总线取消息（1s 超时轮询）
     ├─ /stop  → _handle_stop()    # 取消当前会话所有任务和子 Agent
     ├─ /restart → _handle_restart()  # os.execv 原地重启
     └─ 普通消息 → asyncio.create_task(_dispatch(msg))
                      └─ _processing_lock（全局串行锁）
                          └─ _process_message(msg)
```

> **设计要点**：每条消息被包装为独立的 `asyncio.Task`，主循环不阻塞，始终能响应 `/stop` 命令。`_processing_lock` 保证同一时刻只有一条消息在被处理，避免会话状态竞争。

#### 2.2 ReAct 循环（`_run_agent_loop`）

标准的 **Reason + Act** 迭代模式：

```
while iteration < max_iterations:
    response = LLM(messages, tools)
    if response.has_tool_calls:
        执行工具 → 追加 tool_result → 继续循环
    else:
        final_content = response.content
        break
```

**三种终止条件：**

| 条件 | 说明 |
|------|------|
| ① 正常终止 | LLM 不再调用工具，返回纯文本 |
| ② 错误终止 | `finish_reason == "error"`，不写入 session 历史（防止污染上下文） |
| ③ 超限终止 | 达到 `max_iterations`（默认 40），返回提示语 |

#### 2.3 工具集（ToolRegistry）

主 Agent 注册的默认工具：

| 工具 | 说明 |
|------|------|
| `read_file` / `write_file` / `edit_file` / `list_dir` | 文件系统操作 |
| `exec` | Shell 命令执行 |
| `web_search` / `web_fetch` | 网络搜索与抓取 |
| `message` | 主动向用户发消息（实时进度推送） |
| `spawn` | 派生子 Agent（后台异步执行） |
| `cron` | 定时任务（可选） |
| MCP 工具 | 通过 MCP 协议动态接入的外部工具 |

#### 2.4 两种消息发送方式

Agent 有两种方式向用户发消息，互斥触发：

```
方式 A（被动）：_run_agent_loop 结束 → _process_message 返回 OutboundMessage → _dispatch 统一发出
方式 B（主动）：LLM 在循环中途调用 message 工具 → MessageTool 直接 publish_outbound
```

若方式 B 已触发（`mt._sent_in_turn == True`），`_process_message` 返回 `None`，`_dispatch` 不再重复发送。

---

### 3. SubagentManager（`nanobot/agent/subagent.py`）

管理后台子 Agent 的创建、运行与生命周期。

#### 3.1 子 Agent 的创建（`spawn`）

```python
spawn(task, label, origin_channel, origin_chat_id, session_key)
```

1. 生成短 UUID 作为 `task_id`
2. `asyncio.create_task(_run_subagent(...))` — **立即返回，不阻塞主 Agent**
3. 注册到 `_running_tasks[task_id]`
4. 若有 `session_key`，归属到 `_session_tasks[session_key]`（用于批量取消）
5. 注册 `_cleanup` 回调，任务结束时自动清理引用

#### 3.2 子 Agent 的执行（`_run_subagent`）

子 Agent 拥有**独立的工具集**（不含 `message` 和 `spawn`，避免递归派生），执行标准的 ReAct 循环（最多 15 轮）：

```
构建独立 ToolRegistry
  └─ read_file / write_file / edit_file / list_dir / exec / web_search / web_fetch

while iteration < 15:
    response = LLM(messages, tools)
    if has_tool_calls → 执行工具 → 继续
    else → final_result = response.content → break

_announce_result()  →  bus.publish_inbound(channel="system")
```

#### 3.3 结果回报机制

子 Agent 完成后，通过消息总线将结果注入主 Agent：

```python
InboundMessage(
    channel="system",          # 标识为系统内部消息
    sender_id="subagent",
    chat_id="原始channel:原始chat_id",  # 确保回报到正确的对话
    content="[Subagent '...' completed] ...",
)
```

主 Agent 在 `_process_message` 中识别 `channel == "system"`，解析出真实的 `channel:chat_id`，触发新一轮 LLM 调用，将结果自然语言化后回复用户。

#### 3.4 任务生命周期管理

```
_running_tasks: dict[task_id → asyncio.Task]   # 全局运行表
_session_tasks: dict[session_key → {task_id}]  # 按会话分组

cancel_by_session(session_key)  # /stop 命令触发，批量取消该会话下所有子 Agent
```

---

### 4. SpawnTool（`nanobot/agent/tools/spawn.py`）

LLM 与 SubagentManager 之间的桥梁，工具定义决定了 LLM 的调用时机。

```python
description = (
    "Spawn a subagent to handle a task in the background. "
    "Use this for complex or time-consuming tasks that can run independently. "
    "The subagent will complete the task and report back when done."
)
```

**LLM 根据 description 判断何时使用 `spawn`**：只有任务耗时长、且不依赖结果继续推进时，才会调用。

参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| `task` | string（必填） | 子 Agent 要完成的任务描述 |
| `label` | string（可选） | 任务的简短展示标签 |

---

## 消息流转全景

```
用户发消息
    │
    ▼
渠道适配器 → bus.publish_inbound(InboundMessage)
    │
    ▼
AgentLoop.run() → consume_inbound()
    │
    ▼
asyncio.create_task(_dispatch(msg))
    │
    ▼
_processing_lock → _process_message(msg)
    │
    ├─ 构建 messages（历史 + 当前消息）
    ├─ _run_agent_loop()  ← ReAct 循环
    │       │
    │       ├─ LLM 调用普通工具 → 继续循环
    │       │
    │       └─ LLM 调用 spawn → SubagentManager.spawn()
    │               │
    │               └─ asyncio.create_task(_run_subagent())  ← 后台运行
    │                       │
    │                       └─ 完成后 bus.publish_inbound(channel="system")
    │                               │
    │                               └─ AgentLoop 新轮次处理 → 回复用户
    │
    └─ 返回 OutboundMessage → bus.publish_outbound()
            │
            ▼
        渠道适配器 → 发送给用户
```

---

## 关键设计决策

### 模式一：Fire-and-Forget（当前实现）

主 Agent 派生子 Agent 后**立即返回**，不等待结果。子 Agent 完成后通过消息总线触发新的独立对话轮次。

```
主 Agent ──spawn()──→ 子 Agent（后台运行）
    │                      │
    └─ 立即回复用户          └─ 完成后 publish_inbound → 触发新轮次
```

**优点**：主 Agent 不阻塞，响应快，用户体验好。  
**限制**：主 Agent 无法将子 Agent 的结果作为下一步的直接输入（不支持依赖链）。

---

### 模式二：同步等待（Await）

主 Agent 派生子 Agent 后**阻塞等待**其完成，将结果直接作为当前 ReAct 循环的下一步输入。

```
主 Agent ──spawn()──→ 子 Agent
    │                      │
    └─ await task ─────────┘
              │
         result 注入当前 messages → 继续 ReAct 循环
```

**适用场景**：后续步骤强依赖子 Agent 结果（如：先让子 Agent 爬取数据，再由主 Agent 分析）。  
**实现方式**：`SubagentManager.spawn()` 增加 `wait=True` 参数，改为 `await bg_task` 并返回结果字符串，而非立即返回 task_id。  
**代价**：主 Agent 在等待期间无法响应新消息（`_processing_lock` 持续占用）。

---

### 模式三：DAG 编排（Pipeline）

将多个子 Agent 按**依赖关系**组织成有向无环图，按拓扑顺序依次或并行调度。

```
        ┌─ 子 Agent A ─┐
主 Agent ─┤              ├─→ 子 Agent C（依赖 A+B 的结果）→ 汇总回主 Agent
        └─ 子 Agent B ─┘
```

**适用场景**：复杂多步骤任务，如"并行爬取多个数据源 → 汇总分析 → 生成报告"。  
**实现方式**：引入 `PipelineTool`，接收任务列表和依赖关系，由编排器按顺序 `await` 各阶段子 Agent，将上一阶段结果注入下一阶段的 prompt。  
**代价**：实现复杂度高，需要额外的编排状态管理。

---

### 模式四：事件驱动回调（Event-Driven）

子 Agent 完成后不直接回报主 Agent，而是触发**预定义的回调动作**（如写文件、调 API、发通知），主 Agent 无需感知。

```
主 Agent ──spawn(task, on_complete="write_file:result.txt")──→ 子 Agent
                                                                    │
                                                               完成后执行回调
                                                               （不回报主 Agent）
```

**适用场景**：完全独立的后台任务，结果不需要主 Agent 处理（如：定时备份、异步日志归档）。  
**实现方式**：`spawn()` 增加 `callback` 参数，`_run_subagent` 完成后执行回调而非 `_announce_result`。  
**代价**：主 Agent 对子 Agent 状态完全不可见，调试困难。

### 工具描述驱动决策

LLM 完全依赖工具的 `description` 字段决定是否调用 `spawn`。描述中明确说明"适用于耗时长、可独立运行的任务"，LLM 据此自主判断，无需硬编码规则。

### 会话隔离

每个 `session_key`（`channel:chat_id`）对应独立的会话历史和子 Agent 集合，`/stop` 命令只取消当前会话的任务，不影响其他会话。

### 防止上下文污染

- LLM 返回错误响应时（`finish_reason == "error"`），不写入 session 历史
- 工具返回结果超过 16,000 字符时截断
- 用户消息中的运行时上下文前缀（时间、工作目录）不持久化
- 多模态消息中的 base64 图片替换为 `[image]` 占位符
