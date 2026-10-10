# -*- coding: utf-8 -*-
"""The schedule storage model."""
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from ._base import _RecordBase
from ._session import ChatModelConfig
from ....permission import PermissionMode


class ScheduleSource(str, Enum):
    """The source that created the schedule.

    Attributes:
        USER: Created manually by the user via the UI.
        AGENT: Created automatically by an agent, e.g. via a tool call.
    """

    USER = "USER"
    AGENT = "AGENT"


def _get_local_timezone() -> str:
    """Get the local timezone.

    Returns:
        `str`:
            The local timezone.
    """
    try:
        from tzlocal import get_localzone

        return str(get_localzone())
    except Exception:
        return "UTC"


class ScheduleData(BaseModel):
    """The schedule configuration data."""

    name: str = Field(description="Display name of the schedule.")

    description: str = Field(
        default="",
        description="The description of the schedule, including its purpose, "
        "trigger conditions, etc.",
    )

    enabled: bool = Field(
        default=True,
        description="Whether the schedule is active. Disabled schedules are "
        "retained but will not trigger.",
    )

    timezone: str = Field(
        default=_get_local_timezone(),
        description="IANA timezone name used to evaluate the cron expression, "
        "e.g. 'America/New_York' or 'Asia/Shanghai'.",
    )

    cron_expression: str = Field(
        description="Standard 5-field cron expression, e.g. '0 9 * * 1-5'.",
    )

    started_at: datetime = Field(
        description="The date and time the schedule was started.",
        default_factory=lambda: datetime.now().astimezone(),
    )

    ended_at: datetime | None = Field(
        default=None,
        description="The date and time the schedule was ended.",
    )

    chat_model_config: ChatModelConfig = Field(
        description="Model configuration for the auto-created session.",
    )

    stateful: bool = Field(
        title="Stateful",
        default=False,
        description="Whether consecutive executions share the same session "
        "context. If not, each execution will have its own state.",
    )

    permission_mode: PermissionMode = Field(
        title="Permission mode",
        default=PermissionMode.DONT_ASK,
        description="Permission level for the agent during scheduled "
        "execution. Defaults to DONT_ASK since no user is present to "
        "answer prompts.",
    )

    source: ScheduleSource = Field(
        default=ScheduleSource.USER,
        description="Indicates how this schedule was created.",
    )

    source_session_id: str = Field(
        default="",
        description="The source session identifier, used for resource "
        "retrieval.",
    )

    workflow_id: str | None = Field(
        default=None,
        description=(
            "Target workflow to run on each fire; when set, the "
            "schedule starts a workflow run instead of an agent turn "
            "(the agent/model fields are ignored)."
        ),
    )

    run_input: dict[str, Any] = Field(
        default_factory=dict,
        description="Input handed to each workflow run this schedule starts.",
    )


class ScheduleRecord(_RecordBase):
    """Persisted schedule record."""

    user_id: str = Field(description="Owner user id.")

    agent_id: str = Field(
        description="The agent id that will execute the schedule.",
    )

    data: ScheduleData = Field(description="Schedule configuration.")
