"""Shared recursive types for JSON-compatible data."""

from __future__ import annotations

from typing import TypeAlias

from pydantic import JsonValue as PydanticJsonValue, TypeAdapter

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = PydanticJsonValue
JsonObject: TypeAlias = dict[str, JsonValue]

JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
JSON_OBJECT_ADAPTER: TypeAdapter[JsonObject] = TypeAdapter(JsonObject)

__all__ = [
    "JsonScalar",
    "JsonValue",
    "JsonObject",
    "JSON_VALUE_ADAPTER",
    "JSON_OBJECT_ADAPTER",
]
