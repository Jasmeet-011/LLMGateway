"""Unified request/response schema shared by all provider adapters.

The schema covers only what both Claude and Gemini support. Provider-specific
limits (for example the maximum number of stop sequences or a model's maximum
output tokens) are enforced in the adapters, not here.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["user", "assistant"]
Provider = Literal["claude", "gemini", "mock"]
FinishReason = Literal["stop", "max_tokens", "stop_sequence", "other"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Message(_Model):
    role: Role
    content: Annotated[str, Field(min_length=1)]


class ChatRequest(_Model):
    model: Annotated[str, Field(min_length=1)]
    messages: Annotated[list[Message], Field(min_length=1)]
    system: Annotated[str, Field(min_length=1)] | None = None
    max_tokens: Annotated[int, Field(ge=1)]
    # Claude documents 0-1 for both (newer Claude models reject non-default values;
    # the adapter handles that). Gemini's range is unverified; 0-1 is assumed to fit.
    temperature: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    top_p: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    stop: list[Annotated[str, Field(min_length=1)]] | None = None
    stream: bool = False


class Usage(_Model):
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ChatResponse(_Model):
    id: Annotated[str, Field(min_length=1)]
    provider: Provider
    model: Annotated[str, Field(min_length=1)]
    content: str
    finish_reason: FinishReason
    usage: Usage


class StreamDelta(_Model):
    type: Literal["delta"] = "delta"
    text: str


class StreamEnd(_Model):
    type: Literal["end"] = "end"
    finish_reason: FinishReason
    usage: Usage


StreamEvent = Annotated[StreamDelta | StreamEnd, Field(discriminator="type")]
