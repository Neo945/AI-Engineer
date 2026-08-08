"""Unit tests for the normalized LLM message types."""

from __future__ import annotations

from app.llm.messages import ChatMessage, ChatRole, ToolRequest


def test_chat_message_defaults() -> None:
    message = ChatMessage(role=ChatRole.USER)
    assert message.content == ""
    assert message.tool_call_id is None
    assert message.tool_requests == []


def test_tool_request_generates_unique_ids() -> None:
    first = ToolRequest(name="file_read")
    second = ToolRequest(name="file_read")
    assert first.id != second.id


def test_tool_request_name_is_wire_safe() -> None:
    request = ToolRequest(name="not_a_real_tool")
    assert request.name == "not_a_real_tool"


def test_tool_message_carries_tool_call_id() -> None:
    message = ChatMessage(role=ChatRole.TOOL, content="ok", tool_call_id="call_1")
    assert message.tool_call_id == "call_1"


def test_assistant_message_roundtrips_through_json() -> None:
    original = ChatMessage(
        role=ChatRole.ASSISTANT,
        content="",
        tool_requests=[ToolRequest(id="call_1", name="file_read", arguments={"path": "x.py"})],
    )
    restored = ChatMessage.model_validate_json(original.model_dump_json())
    assert restored == original
    assert restored.tool_requests[0].arguments == {"path": "x.py"}
