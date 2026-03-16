"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import json
import weakref
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanobot.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session, SessionManager


# OpenAI Function Calling 格式的工具定义，随 HTTP 请求体一起发给 LLM。
# LLM 不执行任何代码，只负责按 schema 生成符合格式的 JSON 参数并返回。
# 我们再从 response.tool_calls[0].arguments 中取出参数，写入本地文件。
#
# 数据流向：
#   我们 → LLM : messages（对话内容）+ tools（本工具定义）
#   LLM → 我们 : tool_calls[0].arguments = { history_entry, memory_update }
#
# 字段说明（均为 LLM 输出的参数，非输入）：
#   history_entry  : 本次对话的摘要段落，追加写入 HISTORY.md（可 grep 检索）
#   memory_update  : 更新后的完整长期记忆，覆盖写入 MEMORY.md
#
# properties = 声明参数名称、类型及描述（相当于表单填写项说明）
# required   = 指定哪些参数是必填的
#
# vLLM Guided Decoding：
#   vLLM 收到 tools 字段后，会提取 parameters 中的 JSON Schema，
#   在推理时对每步 token 做 logits masking（将不合法 token 的概率置为 -∞，模型只能从合法 token 中采样 ），
#   强制模型只能输出符合 schema 的 JSON，保证 arguments 结构合法。
#   其他 provider（如普通 OpenAI）依赖模型自身理解，输出格式不保证，
#   因此下方对 arguments 做了 str / list / dict 的防御性类型检查。
_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None

_TOOL_CHOICE_ERROR_MARKERS = (
    "tool_choice",
    "toolchoice",
    "does not support",
    'should be ["none", "auto"]',
)


def _is_tool_choice_unsupported(content: str | None) -> bool:
    """Detect provider errors caused by forced tool_choice being unsupported."""
    text = (content or "").lower()
    return any(m in text for m in _TOOL_CHOICE_ERROR_MARKERS)


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    _MAX_FAILURES_BEFORE_RAW_ARCHIVE = 3

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self._consecutive_failures = 0

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def consolidate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
    ) -> bool:
        """Consolidate the provided message chunk into MEMORY.md + HISTORY.md."""
        if not messages:
            return True

        current_memory = self.read_long_term()
        prompt = f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{self._format_messages(messages)}"""

        chat_messages = [
            {"role": "system", "content": "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."},
            {"role": "user", "content": prompt},
        ]

        try:
            forced = {"type": "function", "function": {"name": "save_memory"}}
            response = await provider.chat_with_retry(
                messages=chat_messages,
                tools=_SAVE_MEMORY_TOOL,
                model=model,
                tool_choice=forced,
            )

            if response.finish_reason == "error" and _is_tool_choice_unsupported(
                response.content
            ):
                logger.warning("Forced tool_choice unsupported, retrying with auto")
                response = await provider.chat_with_retry(
                    messages=chat_messages,
                    tools=_SAVE_MEMORY_TOOL,
                    model=model,
                    tool_choice="auto",
                )

            if not response.has_tool_calls:
                logger.warning(
                    "Memory consolidation: LLM did not call save_memory "
                    "(finish_reason={}, content_len={}, content_preview={})",
                    response.finish_reason,
                    len(response.content or ""),
                    (response.content or "")[:200],
                )
                return self._fail_or_raw_archive(messages)

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("Memory consolidation: unexpected save_memory arguments")
                return self._fail_or_raw_archive(messages)

            if "history_entry" not in args or "memory_update" not in args:
                logger.warning("Memory consolidation: save_memory payload missing required fields")
                return self._fail_or_raw_archive(messages)

            entry = args["history_entry"]
            update = args["memory_update"]

            if entry is None or update is None:
                logger.warning("Memory consolidation: save_memory payload contains null required fields")
                return self._fail_or_raw_archive(messages)

            entry = _ensure_text(entry).strip()
            if not entry:
                logger.warning("Memory consolidation: history_entry is empty after normalization")
                return self._fail_or_raw_archive(messages)

            self.append_history(entry)
            update = _ensure_text(update)
            if update != current_memory:
                self.write_long_term(update)

            self._consecutive_failures = 0
            logger.info("Memory consolidation done for {} messages", len(messages))
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return self._fail_or_raw_archive(messages)

    def _fail_or_raw_archive(self, messages: list[dict]) -> bool:
        """Increment failure count; after threshold, raw-archive messages and return True."""
        self._consecutive_failures += 1
        if self._consecutive_failures < self._MAX_FAILURES_BEFORE_RAW_ARCHIVE:
            return False
        self._raw_archive(messages)
        self._consecutive_failures = 0
        return True

    def _raw_archive(self, messages: list[dict]) -> None:
        """Fallback: dump raw messages to HISTORY.md without LLM summarization."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        self.append_history(
            f"[{ts}] [RAW] {len(messages)} messages\n"
            f"{self._format_messages(messages)}"
        )
        logger.warning(
            "Memory consolidation degraded: raw-archived {} messages", len(messages)
        )


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
    ):
        self.store = MemoryStore(workspace)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive a selected message chunk into persistent memory."""
        return await self.store.consolidate(messages, self.provider, self.model)

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        """
        在会话消息列表中，找到一个「用户轮次边界」，使得归档该边界之前的消息
        能够减少至少 tokens_to_remove 个 token。

        「用户轮次边界」的含义：某条 role=="user" 消息的索引位置。
        归档时会取 [last_consolidated, boundary_idx) 这段消息，
        边界选在用户消息开头，保证归档后剩余的上下文仍以完整的用户轮次开始，
        不会出现孤立的 assistant/tool 消息破坏对话结构。

        返回值：(boundary_idx, removed_tokens)
          - boundary_idx   : 本轮归档的结束索引（不含），即下一轮的起始位置
          - removed_tokens : 归档这段消息预计减少的 token 数
        返回 None 表示找不到合适边界（消息已全部归档，或不足一个完整用户轮次）。
        """
        # 从上次归档结束的位置开始扫描，避免重复处理已归档的消息
        start = session.last_consolidated
        # 前置检查：所有消息已归档，或调用方未要求减少任何 token，直接返回 None
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        # 累计已扫描消息的 token 数，用于判断是否已达到目标减少量
        removed_tokens = 0
        # 记录最近一次满足「用户轮次边界」条件的候选结果，
        # 若遍历结束仍未达到 tokens_to_remove，则返回能减少最多 token 的那个边界
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            # 只在 idx > start 时才允许作为边界：
            # 若 idx == start，归档区间为空（[start, start)），没有意义
            if idx > start and message.get("role") == "user":
                # 更新候选边界：当前用户消息索引 + 截至此处已累计的 token 数
                last_boundary = (idx, removed_tokens)
                # 已累计的 token 数达到目标，立即返回，无需继续扫描
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            # 将当前消息的 token 数累加到计数器（无论是否是边界消息都要累加）
            removed_tokens += estimate_message_tokens(message)

        # 遍历结束仍未达到目标：返回最后一个用户轮次边界（可能为 None）
        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_messages(self, messages: list[dict[str, object]]) -> bool:
        """Archive messages with guaranteed persistence (retries until raw-dump fallback)."""
        if not messages:
            return True
        for _ in range(self.store._MAX_FAILURES_BEFORE_RAW_ARCHIVE):
            if await self.consolidate_messages(messages):
                return True
        return True

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Loop: archive old messages until prompt fits within half the context window."""
        """
        触发条件：当前 prompt 的 token 数超过 context_window_tokens 时触发。
        压缩目标：将 token 数降至 context_window_tokens // 2 以下，为新消息留出空间。
        压缩方式：每轮找到一个"用户轮次边界"，将该边界之前的旧消息批量归档到
                  MEMORY.md（长期记忆）和 HISTORY.md（可检索历史日志），
                  并更新 session.last_consolidated 游标，避免重复归档。
        并发安全：通过 per-session asyncio.Lock 保证同一会话不会并发触发多次压缩。
        最大轮次：由 _MAX_CONSOLIDATION_ROUNDS 限制，防止无限循环。
        """
        # 前置检查：会话无消息或未配置上下文窗口大小时，直接跳过
        if not session.messages or self.context_window_tokens <= 0:
            return

        # 获取该会话的专属锁，防止并发触发多次压缩导致数据竞争
        lock = self.get_lock(session.key)
        async with lock:
            # 目标：将 prompt token 数压缩到上下文窗口的一半以下，留出足够空间给新消息
            target = self.context_window_tokens // 2
            # 估算当前 prompt 的 token 数，source 表示估算来源（如 tiktoken / 字符估算等）
            estimated, source = self.estimate_session_prompt_tokens(session)
            # 估算失败（返回 0）时跳过，避免基于无效数据做压缩决策
            if estimated <= 0:
                return
            # 当前 token 数未超出上下文窗口，无需压缩，记录 debug 日志后返回
            if estimated < self.context_window_tokens:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            # 多轮压缩循环：每轮归档一批旧消息，直到 token 数降至目标值或达到最大轮次
            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                # 已降至目标以下，压缩完成，退出循环
                if estimated <= target:
                    return

                # 找到一个安全的用户轮次边界，使归档该边界之前的消息能减少足够的 token
                # max(1, ...) 确保至少要求减少 1 个 token，避免传入 0 导致边界查找逻辑异常
                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                # 找不到合适边界（如消息全部已归档，或剩余消息不足一个用户轮次）时退出
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    return

                # boundary[0] 是本轮归档的结束索引（不含），即下一轮的起始位置
                end_idx = boundary[0]
                # 取出本轮待归档的消息片段：从上次归档结束位置到本轮边界
                chunk = session.messages[session.last_consolidated:end_idx]
                # 片段为空说明边界与上次归档位置重合，无新内容可归档，退出
                if not chunk:
                    return

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                # 将该片段消息归档到持久化记忆（MEMORY.md + HISTORY.md），失败则中止本次压缩
                if not await self.consolidate_messages(chunk):
                    return
                # 更新会话的已归档偏移量，标记这批消息已被压缩，下次不再重复处理
                session.last_consolidated = end_idx
                # 持久化会话状态，确保 last_consolidated 在重启后仍然有效
                self.sessions.save(session)

                # 重新估算压缩后的 token 数，决定是否需要继续下一轮压缩
                estimated, source = self.estimate_session_prompt_tokens(session)
                # 估算失败时退出，避免死循环
                if estimated <= 0:
                    return
