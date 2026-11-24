import pytest

from doubao_work2api.context import ContextLengthError, apply_context_budget, conversation_groups
from doubao_work2api.models import ChatCompletionRequest


def test_auto_truncation_keeps_system_and_whole_tool_turns():
    messages = [
        {"role": "system", "content": "Keep this instruction"},
        {"role": "user", "content": "x" * 295000},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_old", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_old", "content": "old result"},
        {"role": "user", "content": "latest question"},
    ]
    request = ChatCompletionRequest(messages=messages, truncation="auto")
    bounded = apply_context_budget(request)
    assert [message.content for message in bounded.messages] == ["Keep this instruction", "latest question"]
    assert len(request.messages) == 5
    assert bounded.gateway_context["dropped_turns"] == 1


def test_default_rejects_overflow_and_does_not_silently_drop():
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "x" * 300000}])
    with pytest.raises(ContextLengthError):
        apply_context_budget(request)


def test_latest_turn_and_system_are_never_cut_in_half():
    request = ChatCompletionRequest(messages=[{"role": "system", "content": "x" * 295000}, {"role": "user", "content": "latest"}], truncation="auto")
    with pytest.raises(ContextLengthError):
        apply_context_budget(request)


def test_unknown_model_window_is_not_invented():
    request = ChatCompletionRequest(model="future-unverified-model", messages=[{"role": "user", "content": "hello"}])
    assert apply_context_budget(request).gateway_context["window"] is None


def test_window_sized_max_tokens_does_not_block_large_prompt():
    # ZCode sends max_tokens near the whole window (e.g. 288471) to mean
    # "as much as possible"; the reservation must not starve the prompt.
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "x" * 248000}], max_tokens=288471)
    bounded = apply_context_budget(request)
    assert bounded.gateway_context["requested_output_tokens"] == 288471
    assert bounded.gateway_context["reserved_output_tokens"] == 32768
    assert bounded.gateway_context["estimated_input_tokens"] > 248000


def test_output_reservation_is_capped_before_dispatch():
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "x" * 10000}], max_completion_tokens=290000)
    bounded = apply_context_budget(request)
    assert bounded.gateway_context["reserved_output_tokens"] == 32768
    assert bounded.gateway_context["requested_output_tokens"] == 290000


def test_breakdown_attributes_most_bytes_to_a_large_tool_declaration():
    # A client declaring a hundred MCP tools spends the window on schemas
    # before the user types anything; the metadata must show that.
    tools = [
        {
            "type": "function",
            "function": {
                "name": f"tool_{index}",
                "description": "d" * 2000,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for index in range(50)
    ]
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], tools=tools)
    bounded = apply_context_budget(request)
    breakdown = bounded.gateway_context["estimated_breakdown"]
    assert breakdown["tools_declared"] == 50
    assert breakdown["tools"] > 100000
    assert breakdown["tools"] > breakdown["messages"] * 100


def test_byte_cap_raises_the_declared_window(monkeypatch):
    # The live channel accepted 568 KB while the catalog declares 300000, so
    # the operator can calibrate the byte gate without editing the catalog.
    monkeypatch.setenv("DOUBAO_WORK_CONTEXT_BYTE_CAP", "512000")
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "x" * 300000}])
    bounded = apply_context_budget(request)
    assert bounded.gateway_context["window"] == 512000
    assert bounded.gateway_context["declared_window"] == 300000
