# 02 — Agent 循环：ReAct 模式详解

> 学习笔记 · 源码位置：`nanobot/agent/loop.py`

---

## 什么是 ReAct？

ReAct = **Re**asoning + **Act**ing，翻译过来就是"边想边做"。

人类解决问题时不是一步到位的，而是：想一想 → 做一步 → 看结果 → 再想想 → 再做一步……直到搞定为止。ReAct 就是让 LLM 模仿这个过程。

---

## 核心流程（5分钟看懂）

```python
# 伪代码 —— 对应 loop.py 的 _run_agent_loop() 方法
while 还没超过最大轮次:
    # 1. 问 LLM
    response = LLM.chat(messages, tools)
    
    if LLM 想调用工具:
        # 2. 执行工具，拿到结果
        result = 执行工具(response.tool_calls)
        # 3. 把结果塞回 messages，让 LLM 下一轮能看到
        messages.append(工具结果)
        # 继续循环
    else:
        # 4. LLM 不调工具了，说明想好了，输出最终回答
        最终回答 = response.content
        break
```

就这么简单。整个 Agentic 系统最核心的逻辑，就是这个 while 循环。

---

## 三种"停下来"的方式

| 终止条件 | 什么时候触发 | 怎么处理 |
|---------|------------|---------|
| ✅ 正常终止 | LLM 不再调用工具，直接返回文本 | 记录回答，保存会话 |
| ❌ 错误终止 | LLM 返回 `finish_reason == "error"` | **不写入会话历史**（防止错误"传染"后续对话） |
| ⏰ 超限终止 | 达到 `max_iterations`（默认40轮） | 返回"任务太复杂，请拆分"的提示 |

> **面试重点**：为什么错误响应不写入 session？因为如果写入了，下次加载历史时 LLM 会把错误当作上下文，可能导致永久性 400 循环——这是一个真实的生产 bug（#1303）。

---

## 消息调度：从收到消息到开始循环

```
用户消息到达 Bus
    ↓
AgentLoop.run()  — 不断轮询 Bus
    ↓
收到消息后创建一个 asyncio.Task（异步任务）
    ↓
asyncio.Task → _dispatch() → 加锁 → _process_message()
    ↓
_process_message() 做这些事：
    1. 加载 session 历史
    2. 检查是否需要记忆压缩
    3. 注入工具路由上下文（channel + chat_id）
    4. 用 ContextBuilder 组装完整的 messages
    5. 调用 _run_agent_loop() — 就是上面的 ReAct 循环
    6. 保存本轮新增消息到 session
    7. 返回 OutboundMessage
```

### 为什么要加锁？

`_processing_lock` 是一个全局串行锁，保证同一时刻只有一条消息在被处理。

原因很简单：多条消息同时处理会导致**会话状态竞争**——两个请求同时读同一个 session，各自追加消息，最后互相覆盖。

### 为什么用 Task 而不是直接 await？

因为主循环需要随时响应 `/stop` 命令。如果直接 `await _process_message()`，主循环就卡住了，用户发 `/stop` 也没人处理。

---

## 工具调用的数据流

LLM 不会自己执行工具。它只是"说"：我想调用 read_file，参数是 {"path": "main.py"}。然后由我们的代码执行。

```
LLM 返回 response:
    content: "让我看看这个文件"
    tool_calls: [{id: "call_001", name: "read_file", arguments: {path: "main.py"}}]
        ↓
我们记录 assistant 消息（包含 tool_calls）到 messages
        ↓
执行 read_file("main.py")，得到文件内容
        ↓
追加一条 tool role 的消息到 messages:
    {role: "tool", tool_call_id: "call_001", content: "文件内容..."}
        ↓
下一轮循环，LLM 看到了文件内容，决定下一步做什么
```

> **关键**：`tool_call_id` 是唯一标识符，用来把"请求"和"结果"对应起来。LLM 可以一次返回多个工具调用（parallel tool calls），所以必须用 ID 匹配。

---

## Agent 的两种"说话"方式

Agent 有两种方式向用户发消息，而且互斥：

| 方式 | 触发时机 | 源码位置 |
|------|---------|---------|
| **被动回复** | ReAct 循环结束后，把最终回答包装成 OutboundMessage 返回 | `_process_message()` 返回值 |
| **主动推送** | LLM 在循环中途调用 `message` 工具，直接发消息给用户 | `MessageTool.execute()` |

**防重复机制**：如果 `message` 工具已经发过消息了（`_sent_in_turn == True`），`_process_message` 就返回 `None`，外层的 `_dispatch` 看到 None 就不再发了。

---

## 工具结果截断

工具返回的内容可能非常长（比如一个大文件、一段很长的命令输出），直接塞进 messages 会撑爆 token 上限。所以有一个硬限制：

```python
_TOOL_RESULT_MAX_CHARS = 16_000  # 超过这个长度就截断
```

保存到 session 时也会截断，防止 session 文件膨胀。

---

## 实时进度推送

用户等 Agent 干活时不能只看到空白，需要有"正在思考..."这样的反馈。nanobot 的做法是：

```
LLM 返回工具调用时：
    1. 如果有思考内容（content 字段）→ 推送给用户，比如"让我搜索一下..."
    2. 推送工具调用提示（tool_hint）→ 比如 web_search("Python 教程")
```

这些进度消息带有 `_progress: true` 元数据标记，ChannelManager 会根据配置决定是否发给用户。

---

## 特殊命令处理

AgentLoop 内置了几个特殊命令，不走 ReAct 循环：

| 命令 | 做什么 |
|------|-------|
| `/stop` | 取消当前会话的所有任务 + 子 Agent |
| `/restart` | `os.execv` 原地重启整个进程 |
| `/new` | 归档当前会话记忆，清空会话历史 |
| `/help` | 返回命令列表 |

---

## 数据清洗：`_save_turn` 方法

把本轮消息保存到 session 之前，会做三种清洗：

1. **截断过长的工具结果**：超过 16000 字符的 tool 消息会被截断
2. **剥离运行时上下文**：用户消息头部的时间、工作目录等动态信息不应持久化（下次会重新生成）
3. **替换图片数据**：base64 内联图片体积极大，用 `[image]` 占位符替代

---

## 面试话术

> "Agent Loop 的核心是一个 ReAct 循环。每一轮中，LLM 作为决策中枢，决定是调用工具还是直接回答。我们通过三道防线保证系统稳定：max_iterations 防止无限循环，error 检测防止上下文污染，工具结果截断控制 token 膨胀。消息调度采用 asyncio.Task + 全局串行锁，保证会话状态一致的同时不阻塞主循环对 /stop 命令的响应。"

---

**上一篇 ←** [01-overview.md](./01-overview.md) 全景总览  
**下一篇 →** [03-tool-system.md](./03-tool-system.md) 工具系统详解
