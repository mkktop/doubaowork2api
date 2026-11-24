"""Conservative gateway context budgets; never presented as upstream billing."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from .catalog import request_model
from .errors import GatewayError
from .models import ChatCompletionRequest, ChatMessage
from .request_contract import estimate_tokens, get_output_limit, render_contract_instructions

logger = logging.getLogger(__name__)


class ContextLengthError(GatewayError):
    status_code = 400
    error_type = "invalid_request_error"
    code = "context_length_exceeded"


def conversation_groups(messages: list[ChatMessage]) -> tuple[list[ChatMessage], list[list[ChatMessage]]]:
    """Keep complete user turns, including their function calls and results."""
    systems: list[ChatMessage] = []
    groups: list[list[ChatMessage]] = []
    for message in messages:
        if message.role in {"system", "developer"}:
            systems.append(message)
        else:
            if message.role == "user" or not groups:
                groups.append([])
            groups[-1].append(message)
    return systems, groups


def _message_tokens(messages: list[ChatMessage]) -> int:
    from .protocol import render_prompt

    return estimate_tokens(render_prompt(messages)) + len(messages) * 8


def estimate_request_parts(request: ChatCompletionRequest) -> dict[str, int]:
    """Split the estimate so an oversized tool declaration is visible as such.

    A client that declares a hundred MCP tools spends the whole window on
    schemas before the user types anything; without this breakdown the total
    looks like a conversation problem.
    """
    from .tool_bridge import render_tool_instructions

    tools_text = ""
    if request.tools:
        tools_text = render_tool_instructions(request.tools, request.tool_choice, request.parallel_tool_calls)
    images = sum(1 for message in request.messages if isinstance(message.content, list) for part in message.content if part.get("type") == "image_url")
    return {
        "messages": _message_tokens(request.messages),
        "tools": estimate_tokens(tools_text),
        "contract": estimate_tokens(render_contract_instructions(request)),
        # Image encodings never count as text. The fixed allowance is a budget
        # estimate; the upstream does not expose an exact image tokenizer.
        "images": images * 4096,
        "tools_declared": len(request.tools or []),
    }


def estimate_request_tokens(request: ChatCompletionRequest) -> int:
    parts = estimate_request_parts(request)
    return parts["messages"] + parts["tools"] + parts["contract"] + parts["images"]


def apply_context_budget(request: ChatCompletionRequest) -> ChatCompletionRequest:
    spec = request_model(request)
    window = spec.context_window if spec else None
    parts = estimate_request_parts(request)
    estimated = parts["messages"] + parts["tools"] + parts["contract"] + parts["images"]
    # Only the message history is truncated later; the rest is fixed per turn.
    fixed = parts["tools"] + parts["contract"] + parts["images"]
    requested_output = get_output_limit(request) or 4096
    # Clients such as ZCode send max_tokens near the whole context window
    # (e.g. 288471) to mean "as much as possible". The gateway does not pass
    # max_tokens upstream, it only truncates locally, so reserving the full
    # number leaves almost no room for the actual prompt. Reserve a realistic
    # output budget instead; override with DOUBAO_WORK_OUTPUT_RESERVE_CAP.
    try:
        output_cap = int(os.getenv("DOUBAO_WORK_OUTPUT_RESERVE_CAP", "32768"))
    except ValueError:
        output_cap = 32768
    output = min(requested_output, max(1024, output_cap))
    # The live channel reports a server system prompt around 6K tokens.
    # Reserve more, rather than claiming all of the 300K belongs to the user.
    reserve = 8192
    old = getattr(request, "gateway_context", {})
    previous_dropped = old.get("dropped_turns", 0) if isinstance(old, dict) else 0
    previous_dropped = previous_dropped if type(previous_dropped) is int and previous_dropped >= 0 else 0
    metadata: dict[str, Any] = {"estimated_input_tokens": estimated, "reserved_output_tokens": output, "requested_output_tokens": requested_output, "server_reserve": reserve, "window": window, "dropped_turns": previous_dropped, "estimated_breakdown": parts, "is_estimated": True}
    if window is None:
        if estimated >= 100_000:
            logger.info("context budget: %d estimated (messages %d, tools %d in %d declarations, contract %d, images %d); window unknown", estimated, parts["messages"], parts["tools"], parts["tools_declared"], parts["contract"], parts["images"])
        return request.model_copy(update={"gateway_context": metadata})
    available = window - reserve - output
    if estimated >= 100_000:
        logger.info("context budget: %d estimated of %d available (messages %d, tools %d in %d declarations, contract %d, images %d); window %d, reserved output %d, server reserve %d", estimated, available, parts["messages"], parts["tools"], parts["tools_declared"], parts["contract"], parts["images"], window, output, reserve)
    if estimated <= available:
        return request.model_copy(update={"gateway_context": metadata})
    if request.truncation != "auto":
        raise ContextLengthError(
            "Estimated context exceeds the verified model window: "
            f"{estimated} bytes estimated, {available} bytes available "
            f"(window {window}, reserved output {output}, server reserve {reserve}). "
            f"Of the estimate, tools account for {parts['tools']} bytes in "
            f"{parts['tools_declared']} declarations and messages for "
            f"{parts['messages']} bytes. "
            "Use truncation='auto' or /v1/responses/compact."
        )
    systems, groups = conversation_groups(request.messages)
    dropped = 0
    while len(groups) > 1:
        groups.pop(0)
        dropped += 1
        candidate = request.model_copy(update={"messages": systems + [message for group in groups for message in group]})
        estimated = fixed + _message_tokens(candidate.messages)
        if estimated <= available:
            metadata.update(estimated_input_tokens=estimated, dropped_turns=previous_dropped + dropped, estimated_breakdown={**parts, "messages": estimated - fixed})
            return candidate.model_copy(update={"gateway_context": metadata})
    raise ContextLengthError("The system instructions and latest complete turn exceed the verified model window; they cannot be safely truncated.")
