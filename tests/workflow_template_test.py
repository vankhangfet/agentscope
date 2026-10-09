# -*- coding: utf-8 -*-
"""The mustache-lite renderer over the run blackboard."""
from unittest import TestCase

from agentscope.app._service._workflow_template import (
    RenderError,
    render_template,
)


CONTEXT = {
    "input": {"topic": "Q3 revenue"},
    "nodes": {
        "research": {"output": {"text": "found 12 sources"}},
        "check": {"output": {"structured": {"passed": True}}},
    },
}


class RenderTemplateTest(TestCase):
    """Substitution, resolution and errors."""

    def test_plain_text_passes_through(self) -> None:
        self.assertEqual(render_template("no tags", CONTEXT), "no tags")

    def test_input_paths(self) -> None:
        self.assertEqual(
            render_template("Report on {{input.topic}}", CONTEXT),
            "Report on Q3 revenue",
        )

    def test_node_output_paths(self) -> None:
        self.assertEqual(
            render_template(
                "Sources: {{nodes.research.output.text}}",
                CONTEXT,
            ),
            "Sources: found 12 sources",
        )

    def test_nested_structured_paths(self) -> None:
        self.assertEqual(
            render_template(
                "{{nodes.check.output.structured.passed}}",
                CONTEXT,
            ),
            "true",
        )

    def test_item_placeholder(self) -> None:
        self.assertEqual(
            render_template("{{item}}", {"item": "apple"}),
            "apple",
        )

    def test_missing_path_raises_render_error(self) -> None:
        with self.assertRaises(RenderError) as ctx:
            render_template("{{nodes.ghost.output.text}}", CONTEXT)
        self.assertIn("nodes.ghost", str(ctx.exception))

    def test_none_renders_as_empty(self) -> None:
        context = {"input": {"topic": None}}
        self.assertEqual(
            render_template("[{{input.topic}}]", context),
            "[]",
        )

    def test_dict_renders_as_compact_json(self) -> None:
        rendered = render_template(
            "{{nodes.research.output}}",
            CONTEXT,
        )
        self.assertEqual(
            rendered,
            '{"text": "found 12 sources"}',
        )

    def test_int_renders_as_str(self) -> None:
        context = {"input": {"count": 3}}
        self.assertEqual(render_template("{{input.count}}", context), "3")
