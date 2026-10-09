# -*- coding: utf-8 -*-
"""Mustache-style rendering of node inputs over the run blackboard."""
import json
import re
from typing import Any
from typing import Match


class RenderError(ValueError):
    """A template referenced a path the blackboard does not hold."""


_TAG = re.compile(r"{{\s*([a-zA-Z0-9_.]+)\s*}}")


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
        path = match.group(1)
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
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    return _TAG.sub(_resolve, template)
