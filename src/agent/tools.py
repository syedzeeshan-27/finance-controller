"""Strict tool definitions for the agent loop.

Every tool the agent sees is built through `strict_tool`, which guarantees:
`strict: true`, `additionalProperties: false`, and every property required.
The API then validates tool inputs against the schema exactly, so tool
implementations never need defensive parsing.
"""

from __future__ import annotations


def strict_tool(name: str, description: str, properties: dict,
                required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": sorted(required if required is not None
                               else properties.keys()),
            "additionalProperties": False,
        },
    }


def assert_strict(tool: dict) -> None:
    """Raise AssertionError unless the tool honours the strict contract.
    Used by tests over every schema the agents ship."""
    assert tool.get("strict") is True, tool.get("name")
    schema = tool["input_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == sorted(schema["properties"].keys())
