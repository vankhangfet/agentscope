# -*- coding: utf-8 -*-
"""The built-in integration presets — a catalog that needs no hub.

Each preset is a curated MCP server plus the few fields a user must
fill in, typically a URL and a token. Rendering is direct: every
filled-in field becomes one environment variable on a fixed stdio
command, with no template substitution and no network. Kept as plain
data so the catalog can be listed — and later re-keyed — without any
hub being registered.
"""
from dataclasses import dataclass

from ...mcp import MCPClient, StdioMCPConfig


@dataclass(frozen=True)
class PresetField:
    """One input a preset asks the user for.

    ``env_var`` is where a filled-in value lands in the spawned
    server's environment. Declaring it per field, rather than deriving
    it from position, keeps the catalog entries self-describing.
    """

    name: str
    label: str
    env_var: str
    secret: bool
    placeholder: str = ""
    default: str = ""


@dataclass(frozen=True)
class IntegrationPreset:
    """One catalog entry: the fields to ask and the command to run."""

    id: str
    name: str
    description: str
    fields: list[PresetField]
    command: str
    args: list[str]


PRESETS: list[IntegrationPreset] = [
    IntegrationPreset(
        id="gitlab",
        name="GitLab",
        description=(
            "GitLab projects, issues and merge requests over the API."
        ),
        fields=[
            PresetField(
                name="url",
                label="Server URL",
                env_var="GITLAB_API_URL",
                secret=False,
                placeholder="API URL, e.g. https://gitlab.com/api/v4",
            ),
            PresetField(
                name="token",
                label="Personal Access Token",
                env_var="GITLAB_PERSONAL_ACCESS_TOKEN",
                secret=True,
            ),
        ],
        command="npx",
        args=["-y", "@zereight/mcp-gitlab"],
    ),
    IntegrationPreset(
        id="redmine",
        name="Redmine",
        description="Redmine projects, issues and time entries.",
        fields=[
            PresetField(
                name="url",
                label="Server URL",
                env_var="REDMINE_URL",
                secret=False,
                placeholder="e.g. https://redmine.yourco.example",
            ),
            PresetField(
                name="token",
                label="API Key",
                env_var="REDMINE_API_KEY",
                secret=True,
            ),
        ],
        command="uvx",
        # The published stdio server is ``mcp-redmine`` (runekaagaard),
        # not ``mcp-server-redmine`` — the latter name is not on PyPI, so
        # ``uvx`` failed to resolve it and the server never started. This
        # one speaks stdio by default (matching the stateful stdio client
        # below), reads ``REDMINE_URL`` / ``REDMINE_API_KEY`` exactly as
        # these fields export them, and supports subpath URLs such as
        # ``http://host/redmine``.
        args=["--from", "mcp-redmine", "mcp-redmine"],
    ),
]


def get_preset(preset_id: str) -> IntegrationPreset | None:
    """Return the preset registered under ``preset_id``.

    Args:
        preset_id (`str`):
            The id to look up.

    Returns:
        `IntegrationPreset | None`:
            The matching preset, or ``None`` when the catalog holds no
            such id.
    """
    return next((p for p in PRESETS if p.id == preset_id), None)


def render_preset(
    preset: IntegrationPreset,
    values: dict[str, str],
    name: str | None = None,
) -> MCPClient:
    """Turn a preset plus the user's answers into a connectable client.

    Every field whose value is non-empty is exported under its
    ``env_var``; an empty one is omitted so an unset variable is not
    exported at all. The server always runs stateful — stdio MCPs must
    be.

    Args:
        preset (`IntegrationPreset`):
            The catalog entry carrying the command and field mapping.
        values (`dict[str, str]`):
            The user-supplied values keyed by field name.
        name (`str | None`, optional):
            The name for the installed client. Defaults to the preset
            id.

    Returns:
        `MCPClient`:
            The client ready to hand to ``workspace.add_mcp``.

    Raises:
        `ValueError`:
            When ``name`` is rejected by ``MCPClient`` — anything
            outside ``[a-zA-Z0-9_-]``.
    """
    return MCPClient(
        name=name or preset.id,
        is_stateful=True,
        mcp_config=StdioMCPConfig(
            command=preset.command,
            args=list(preset.args),
            env={
                field.env_var: values[field.name]
                for field in preset.fields
                if values.get(field.name)
            },
        ),
    )


def preset_view(preset: IntegrationPreset) -> dict:
    """Project a preset onto its JSON shape for the catalog listing.

    The field view carries everything a form needs to render and mask
    an input, and nothing about where the value will land — the env
    mapping is server-side detail.
    """
    return {
        "id": preset.id,
        "name": preset.name,
        "description": preset.description,
        "fields": [
            {
                "name": field.name,
                "label": field.label,
                "secret": field.secret,
                "placeholder": field.placeholder,
                "default": field.default,
            }
            for field in preset.fields
        ],
    }
