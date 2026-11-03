"""Bounded response metadata. Never contains generated text or reasoning."""

from typing import Literal

from pydantic import BaseModel, Field

Status = Literal["completed", "incomplete", "failed", "in_progress", "queued", "cancelled",
                 "other"]


class OutputItemMetadata(BaseModel):
    type: Literal["message", "reasoning", "function_call", "other"]
    status: Status | None = None
    role: Literal["assistant", "user", "system", "developer", "other"] | None = None
    phase: Literal["commentary", "final_answer", "other"] | None = None
    content_types: list[Literal["output_text", "refusal", "reasoning_text", "other"]] = []
    output_text_chars: int = 0


class ResponseMetadata(BaseModel):
    json_envelope_valid: bool
    response_status: Status | None = None
    incomplete_reason: Literal["max_output_tokens", "content_filter", "other"] | None = None
    reasoning_tokens: int | None = Field(default=None, ge=0, le=1_000_000_000)
    output_token_cap_reached: bool = False  # Observation, not proof of truncation.
    has_error: bool = False
    has_choices: bool = False
    output_kind: Literal["missing", "list", "other"] = "missing"
    output_items_count: int = 0
    output_items: list[OutputItemMetadata] = Field(default_factory=list, max_length=16)
    output_items_truncated: bool = False
    parse_failure: Literal["json_syntax", "json_recursion", "json_value",
                           "envelope_or_output"] | None = None
