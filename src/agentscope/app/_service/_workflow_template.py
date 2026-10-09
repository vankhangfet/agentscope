# -*- coding: utf-8 -*-
"""Mustache-style rendering of node inputs over the run blackboard.

The dotted-path walk and the scalar rendering live as their own
functions because the workflow rule matcher resolves blackboard
paths — and compares bare-word literals against rendered values —
the very same way.
"""
import json
import re
from typing import Any
from typing import Match


class RenderError(ValueError):
    """A template referenced a path the blackboard does not hold."""


_TAG = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")


def resolve_path(path: str, context: dict[str, Any]) -> Any:
    """Walk a dotted path through nested dicts.

    Args:
        path (`str`): The dotted path to walk.
        context (`dict[str, Any]`): The mapping to walk through.

    Returns:
        `Any`: The value the path names.

    Raises:
        `RenderError`: A step is missing, or not a dict to step
            into.
    """
    value: Any = context
    walked: list[str] = []
    for part in path.split("."):
        walked.append(part)
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise RenderError(
                f"Template path '{path}' cannot be resolved: "
                f"no {part!r} under "
                f"{'.'.join(walked[:-1]) or 'the context root'}.",
            )
    return value


def scalar_text(value: Any) -> str:
    """Render a value the way a template tag would.

    None renders as the empty string, booleans lower-cased, dicts
    and lists as compact JSON.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_template(template: str, context: dict[str, Any]) -> str:
    """Substitute ``{{dotted.path}}`` tags from ``context``.

    Scalars render as strings (booleans lower-cased), dicts and lists
    as compact JSON, and None as the empty string — so a template can
    splice whole node outputs into a later node's prompt.

    Args:
        template (`str`): The template text.
        context (`dict[str, Any]`): The blackboard — ``input``,
            ``nodes``, and (inside loops) ``item``.

    Returns:
        `str`: The rendered text.

    Raises:
        `RenderError`: A tag resolves to nothing in ``context``.
    """

    def _resolve(match: Match[str]) -> str:
        return scalar_text(resolve_path(match.group(1), context))

    return _TAG.sub(_resolve, template)
