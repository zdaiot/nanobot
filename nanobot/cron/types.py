"""Cron types."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class CronSchedule:
    """Schedule definition for a cron job."""
    kind: Literal["at", "every", "cron"]
    # For "at": timestamp in ms
    at_ms: int | None = None
    # For "every": interval in ms
    every_ms: int | None = None
    # For "cron": cron expression (e.g. "0 9 * * *")
    expr: str | None = None
    # Timezone for cron expressions
    tz: str | None = None


@dataclass
class CronPayload:
    """What to do when the job runs."""
    """定时任务触发时的执行指令。

    描述"任务到期后要做什么"，包括：
    - 以何种方式驱动 Agent（kind）
    - 传给 Agent 的指令内容（message）
    - 是否将 Agent 的回复主动推送给用户（deliver / channel / to）
    """

    # 执行类型：
    #   "agent_turn"   — 将 message 作为用户消息交给 Agent 处理（最常用）
    #   "system_event" — 系统内部事件，暂未使用
    kind: Literal["system_event", "agent_turn"] = "agent_turn"

    # 触发时传给 Agent 的指令文本，例如 "提醒我喝水" / "生成今日日报"
    message: str = ""

    # Deliver response to channel
    # 是否将 Agent 的回复主动推送给用户。
    # False（默认）：Agent 静默执行，不主动通知任何人。
    # True：执行完成后通过 channel / to 指定的渠道将回复投递出去。
    deliver: bool = False

    # 推送渠道，deliver=True 时生效，例如 "whatsapp"、"cli"
    channel: str | None = None  # e.g. "whatsapp"

    # 推送目标，deliver=True 时生效，例如手机号、chat_id
    to: str | None = None  # e.g. phone number


@dataclass
class CronJobState:
    """Runtime state of a job."""
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: Literal["ok", "error", "skipped"] | None = None
    last_error: str | None = None


@dataclass
class CronJob:
    """A scheduled job."""
    id: str
    name: str
    enabled: bool = True
    schedule: CronSchedule = field(default_factory=lambda: CronSchedule(kind="every"))
    payload: CronPayload = field(default_factory=CronPayload)
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = 0
    updated_at_ms: int = 0
    delete_after_run: bool = False


@dataclass
class CronStore:
    """Persistent store for cron jobs."""
    version: int = 1
    jobs: list[CronJob] = field(default_factory=list)
