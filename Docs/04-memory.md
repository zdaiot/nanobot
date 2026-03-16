# 04 — 记忆系统：让 Agent 记住你是谁

> 学习笔记 · 源码位置：`nanobot/agent/memory.py`、`nanobot/session/manager.py`

---

## 为什么需要记忆？

LLM 有一个天然硬伤：**上下文窗口有限**。

比如一个 65K token 的模型，system prompt 占了 3K，工具定义占了 5K，剩余可用大约 57K。如果聊了 100 轮，每轮平均 1K token，那就有 100K token 的对话历史——根本塞不下。

怎么办？和人一样：**旧的事情记个摘要，细节忘掉，但重要的事情要记住**。

---

## 双层记忆架构

nanobot 的记忆分两层，就像人的大脑：

```
┌─────────────────────────────────────┐
│  短期记忆 — Session History          │  ← session/manager.py
│                                     │
│  · 完整的多轮对话消息                  │
│  · 存在 sessions/*.jsonl 文件里       │
│  · 按 session_key 隔离（一个用户一个）  │
│  · 超限时会被压缩归档                  │
└─────────────────────────────────────┘
                  ↓ 压缩
┌─────────────────────────────────────┐
│  长期记忆 — MEMORY.md + HISTORY.md   │  ← agent/memory.py
│                                     │
│  · MEMORY.md：关键事实的总结           │
│    （如"用户喜欢 Python，在做推荐系统"）  │
│  · HISTORY.md：可搜索的对话日志         │
│    （每条以 [YYYY-MM-DD HH:MM] 开头）  │
└─────────────────────────────────────┘
```

**打个比方**：短期记忆就是你的"工作台"，上面摆着当前正在用的材料；长期记忆就是你的"笔记本"，记录了重要的结论和关键信息。工作台太满了，就把旧材料总结一下扔进笔记本。

---

## 短期记忆：Session

### 数据结构

```python
class Session:
    key: str                    # "telegram:12345" — 一个用户一个
    messages: list[dict]        # 完整的对话消息列表（append-only）
    last_consolidated: int      # 已归档到哪条消息了（偏移量）
```

### get_history() — 给 LLM 看的历史

不是把所有 messages 都给 LLM，而是只给 `last_consolidated` 之后的部分（已归档的不再重复传入）：

```python
def get_history(self):
    # 只取未归档的消息
    unconsolidated = self.messages[self.last_consolidated:]
    # 对齐到第一条 user 消息（避免孤立的 tool_result）
    for i, m in enumerate(sliced):
        if m["role"] == "user":
            return sliced[i:]
```

### 持久化格式：JSONL

一个 session 文件长这样：

```
{"_type": "metadata", "key": "telegram:12345", "last_consolidated": 50, ...}
{"role": "user", "content": "帮我看看 main.py", "timestamp": "2024-01-01T10:00:00"}
{"role": "assistant", "content": "好的，让我读一下...", "tool_calls": [...]}
{"role": "tool", "tool_call_id": "call_001", "content": "文件内容..."}
...
```

第一行是元数据（包含 `last_consolidated` 偏移量），后面每行一条消息。

---

## 长期记忆：MemoryStore

### 两个文件

- **MEMORY.md**：长期事实总结，每次更新都是**覆盖写入**整个文件
- **HISTORY.md**：对话日志，每次**追加写入**新条目

### 记忆压缩的触发和流程

```
                     maybe_consolidate_by_tokens()
                              ↓
         估算当前 prompt 的 token 数
                              ↓
         超过 context_window_tokens？
            没超过 → 不做任何事
            超过了 ↓
                              ↓
         目标：压缩到 context_window_tokens / 2
                              ↓
         pick_consolidation_boundary()
         找到一个"用户轮次边界"
            （在某条 user 消息处切割，保证不会切在
             assistant + tool 调用的中间，破坏对话结构）
                              ↓
         取出切割点之前的消息 → 交给 LLM 总结
                              ↓
         LLM 调用 save_memory 工具输出总结结果
                              ↓
         history_entry → 追加到 HISTORY.md
         memory_update → 覆盖 MEMORY.md
                              ↓
         更新 session.last_consolidated 偏移量
         下次 get_history() 就不会再返回这些消息了
```

---

## 最巧妙的设计：LLM 自己做记忆总结

普通的做法是写一段代码来总结对话，但 nanobot 的做法更聪明——**让 LLM 自己总结，通过工具调用返回结构化结果**。

具体做法：

1. 构造一条"记忆压缩请求"发给 LLM
2. 同时传入一个 `save_memory` 工具的定义
3. 用 `tool_choice` 强制 LLM 必须调用这个工具
4. LLM 返回 `save_memory(history_entry="...", memory_update="...")`
5. 我们从返回值中提取两个字段，分别写入 HISTORY.md 和 MEMORY.md

```python
# 强制 LLM 调用 save_memory 工具
forced = {"type": "function", "function": {"name": "save_memory"}}
response = await provider.chat_with_retry(
    messages=chat_messages,
    tools=_SAVE_MEMORY_TOOL,
    tool_choice=forced,    # ← 关键：强制调用
)
```

> 为什么这样做？因为 LLM 比任何规则引擎都更擅长理解对话内容、提取关键信息、生成自然语言总结。而且通过 `tool_choice` 强制调用，保证输出格式是结构化的 JSON，方便我们解析。

---

## 降级策略：Raw Archive

如果 LLM 的记忆总结连续失败 3 次（比如模型抽风了），系统不会卡死，而是启用降级方案：

```python
_MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

def _fail_or_raw_archive(self, messages):
    self._consecutive_failures += 1
    if self._consecutive_failures < 3:
        return False  # 还没到上限，让调用方重试
    # 到上限了，直接把原始消息 dump 到 HISTORY.md
    self._raw_archive(messages)
    return True
```

这是**防御性编程**的典范：宁可记忆质量低一点（raw dump），也不能让系统因为记忆压缩失败而停止工作。

---

## 并发安全

同一个 session 的记忆压缩不能并发执行（否则两次压缩可能处理重叠的消息），所以每个 session 有一个独立的 asyncio.Lock：

```python
_locks: WeakValueDictionary[str, asyncio.Lock]

def get_lock(self, session_key):
    return self._locks.setdefault(session_key, asyncio.Lock())
```

用 `WeakValueDictionary` 是为了让不活跃的 session 的锁能被自动垃圾回收。

---

## 面试话术

> "记忆系统本质上解决的是'LLM 有限上下文窗口 vs 无限对话长度'的矛盾。我的方案是双层设计：短期记忆用完整的 session history 保存在 JSONL 文件里；长期记忆用 LLM 自己做摘要压缩。当 token 超限时，自动触发 consolidation，让一个 Memory Agent 把旧对话总结后归档到 MEMORY.md 和 HISTORY.md。整个过程是渐进式的——不是一次性全部压缩，而是每次压缩一个用户轮次边界的 chunk，直到 token 降到目标以下。失败 3 次后降级为 raw archive，保证系统永不阻塞。"

---

**上一篇 ←** [03-tool-system.md](./03-tool-system.md) 工具系统  
**下一篇 →** [05-multi-agent.md](./05-multi-agent.md) 多 Agent 协作
