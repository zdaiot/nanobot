# 03 — 工具系统：Agent 的"双手"

> 学习笔记 · 源码位置：`nanobot/agent/tools/` 目录

---

## 工具系统是什么？

如果说 LLM 是 Agent 的"大脑"，那工具就是它的"双手"。LLM 自己不能读文件、不能执行命令、不能上网搜索——它只能"说"自己想做什么，然后由工具系统替它执行。

---

## 整体设计：三个核心角色

```
LLM（大脑）
  "我想读 main.py"
       ↓
ToolRegistry（调度中心）
  找到 read_file 工具 → 校验参数 → 调用执行
       ↓
Tool（具体执行者）
  ReadFileTool.execute(path="main.py") → 返回文件内容
```

---

## 第一个角色：Tool 基类

每个工具都继承自 `Tool` 抽象基类（`tools/base.py`），必须实现 4 样东西：

| 要实现的 | 说明 | 示例 |
|---------|------|------|
| `name` | 工具名，LLM 调用时用这个名字 | `"read_file"` |
| `description` | 干什么的，LLM 根据这段话决定何时使用 | `"Read a file from disk"` |
| `parameters` | JSON Schema 格式的参数定义 | `{"type": "object", "properties": {"path": {"type": "string"}}}` |
| `execute()` | 实际执行逻辑，返回字符串结果 | 读文件并返回内容 |

### 参数处理链

LLM 返回的参数可能类型不对（比如数字传成了字符串），所以执行前有一条处理链：

```
LLM 传来参数 → cast_params（类型转换）→ validate_params（校验）→ execute（执行）
```

- **cast_params**：按 JSON Schema 做安全类型转换，比如把 `"42"` 转成 `42`
- **validate_params**：检查必填项、类型、范围、枚举值等
- 校验失败会返回错误信息给 LLM，让它重试

### `to_schema()` 方法

把工具定义转换成 OpenAI Function Calling 的标准格式：

```json
{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from disk",
        "parameters": { ... JSON Schema ... }
    }
}
```

这个格式会随 HTTP 请求发给 LLM，LLM 根据它来决定是否调用、怎么填参数。

---

## 第二个角色：ToolRegistry

`ToolRegistry`（`tools/registry.py`）是一个简单的注册表，核心就是一个字典：

```python
_tools: dict[str, Tool] = {}   # 工具名 → 工具实例
```

| 方法 | 做什么 |
|------|-------|
| `register(tool)` | 注册一个工具 |
| `unregister(name)` | 移除一个工具 |
| `get_definitions()` | 导出所有工具的 JSON Schema（发给 LLM） |
| `execute(name, params)` | 按名字找到工具，校验参数，执行 |

### 错误处理的小巧思

`execute` 方法在工具报错时，会在错误信息末尾追加一句提示：

```python
_HINT = "\n\n[Analyze the error above and try a different approach.]"
```

这句话是给 LLM 看的！LLM 看到这个提示后，会分析错误原因并换一种方式重试，而不是傻傻地重复同样的错误调用。

---

## 第三个角色：具体的工具们

nanobot 内置了这些工具：

### 文件操作（filesystem.py）

| 工具 | 作用 |
|------|------|
| `read_file` | 读文件内容 |
| `write_file` | 写文件（覆盖） |
| `edit_file` | 编辑文件的某一部分（搜索替换） |
| `list_dir` | 列出目录内容 |

**安全设计**：如果配置了 `restrict_to_workspace=True`，所有文件操作都被限制在工作目录内，防止 Agent 乱改系统文件。

### Shell 命令（shell.py）

| 工具 | 作用 |
|------|------|
| `exec` | 执行 Shell 命令 |

**安全设计**：
- `timeout` 参数防止命令挂起（默认60秒）
- 如果开启了 `restrict_to_workspace`，命令只能在工作目录内执行

### 网络操作（web.py）

| 工具 | 作用 |
|------|------|
| `web_search` | 网络搜索（支持 Brave、Tavily、DuckDuckGo 等） |
| `web_fetch` | 抓取网页内容 |

### 消息工具（message.py）

| 工具 | 作用 |
|------|------|
| `message` | 主动给用户发消息 |

这是一个"有状态"的工具，需要知道**发给谁**。所以每次处理消息前，会通过 `set_context(channel, chat_id)` 注入路由信息。

### 子 Agent 工具（spawn.py）

| 工具 | 作用 |
|------|------|
| `spawn` | 派生一个子 Agent 在后台执行任务 |

### 定时任务（cron.py）

| 工具 | 作用 |
|------|------|
| `cron` | 创建/删除/列出定时任务 |

### MCP 工具（mcp.py）

通过 MCP（Model Context Protocol）协议动态接入外部工具服务器，实现"插件式"扩展。

---

## 工具注册流程

AgentLoop 初始化时，在 `_register_default_tools()` 中一次性注册所有默认工具：

```python
def _register_default_tools(self):
    # 文件操作
    self.tools.register(ReadFileTool(workspace=self.workspace))
    self.tools.register(WriteFileTool(workspace=self.workspace))
    self.tools.register(EditFileTool(workspace=self.workspace))
    self.tools.register(ListDirTool(workspace=self.workspace))
    # Shell
    self.tools.register(ExecTool(working_dir=str(self.workspace)))
    # 网络
    self.tools.register(WebSearchTool(config=self.web_search_config))
    self.tools.register(WebFetchTool())
    # 消息
    self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
    # 子 Agent
    self.tools.register(SpawnTool(manager=self.subagents))
    # 定时任务（可选）
    if self.cron_service:
        self.tools.register(CronTool(self.cron_service))
```

> 注意看：每个工具实例化时传入的参数不同。文件工具需要 workspace 路径，网络工具需要搜索配置，消息工具需要发送回调。这就是**依赖注入**的思想。

---

## 工具上下文注入

有三个工具需要知道"当前消息是谁发的"：`message`、`spawn`、`cron`。它们需要路由信息才能把结果发回正确的地方。

```python
def _set_tool_context(self, channel, chat_id, message_id=None):
    for name in ("message", "spawn", "cron"):
        if tool := self.tools.get(name):
            if hasattr(tool, "set_context"):
                tool.set_context(channel, chat_id, ...)
```

这个方法在每次处理新消息时调用，把当前消息的路由信息注入到这些工具中。

---

## 设计模式总结

| 模式 | 在哪里用的 | 好处 |
|------|-----------|------|
| **Strategy 模式** | 每个 Tool 是一个 Strategy | 统一接口，不同实现，互相替换不影响 |
| **Registry 模式** | ToolRegistry | 动态注册/发现/执行，新增工具零改动 |
| **依赖注入** | 工具实例化时传入配置/回调 | 工具不依赖全局状态，方便测试 |
| **Template Method** | Tool 基类的 cast_params → validate → execute | 统一处理流程，子类只关心执行逻辑 |

---

## 怎么加一个新工具？（3步）

```python
# 第1步：继承 Tool 基类
class MyNewTool(Tool):
    @property
    def name(self):
        return "my_tool"
    
    @property
    def description(self):
        return "做一些很酷的事情"
    
    @property
    def parameters(self):
        return {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "输入内容"}
            },
            "required": ["input"]
        }
    
    async def execute(self, input: str) -> str:
        return f"处理结果: {input}"

# 第2步：在 AgentLoop 里注册
self.tools.register(MyNewTool())

# 第3步：没有第3步了，LLM 会根据 description 自动判断何时使用
```

---

## 面试话术

> "工具系统采用 Strategy + Registry 双模式。每个工具是一个 Strategy（统一接口不同实现），Registry 负责动态注册和分发。LLM 调用工具时只需指定 name 和 arguments，Registry 负责路由、参数校验、执行和错误包装。新增工具只需三步：继承基类、实现4个方法、注册到 Registry，完全不用改框架代码。"

---

**上一篇 ←** [02-agent-loop.md](./02-agent-loop.md) Agent 循环  
**下一篇 →** [04-memory.md](./04-memory.md) 记忆系统详解
