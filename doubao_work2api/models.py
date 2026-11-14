from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, PrivateAttr, model_validator

from .catalog import DEFAULT_MODEL_ID, normalize_model
from .compatibility import without_client_hints


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool", "function"]
    content: str | list[dict[str, Any]] | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    _model_spec: Any = PrivateAttr(default=None)
    _affinity_scope: str = PrivateAttr(default='')
    _conversation_hint: str = PrivateAttr(default='')
    _official_usage: Any = PrivateAttr(default=None)
    _verification_probe_id: str = PrivateAttr(default='')
    _native_reused: bool = PrivateAttr(default=False)

    model: str = DEFAULT_MODEL_ID
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: StrictInt | None = None
    max_completion_tokens: StrictInt | None = None
    stop: str | list[str] | None = None
    user: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    reasoning_effort: str | int | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    truncation: Literal["disabled", "auto"] = "disabled"
    stream_options: dict[str, Any] | None = None

    @model_validator(mode='before')
    @classmethod
    def ignore_client_hints(cls, value: Any) -> Any:
        return without_client_hints(value)

    @property
    def normalized_model(self) -> str:
        return normalize_model(self.model or "")


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str = "doubao"
    context_window: int | None = None
    reasoning_efforts: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None
    image_input: bool | None = None


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]
