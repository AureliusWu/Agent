"""Bounded JSON validation for the public Provider structured-output contract.

This is deliberately NOT a complete JSON Schema implementation. Supported
assertions are type, properties, required, additionalProperties, items,
min/maxItems, min/maxLength, minimum/maximum, exclusiveMinimum/Maximum, enum,
and const. title/description/$comment/default/examples are annotations only.
References, regex/format, composition, dialect declarations and every unknown
keyword are rejected before calling a model. No references are ever fetched.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, NoReturn


MAX_SCHEMA_BYTES = 131_072
MAX_SCHEMA_DEPTH = 32
MAX_SCHEMA_NODES = 2_000
MAX_OUTPUT_BYTES = 1_048_576
MAX_OUTPUT_DEPTH = 64
MAX_OUTPUT_NODES = 20_000
MAX_VALIDATION_STEPS = 100_000

_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_LENGTHS = {"minItems", "maxItems", "minLength", "maxLength"}
_NUMBERS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"}
_ANNOTATIONS = {"title", "description", "$comment", "default", "examples"}
_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum", "const"} | _LENGTHS | _NUMBERS | _ANNOTATIONS


class StructuredOutputError(ValueError):
    """Only fixed public messages: never include a value, path or schema key."""

    def __init__(self, code: str):
        self.code = code
        super().__init__({
            "unsupported_schema": "结构化输出 Schema 使用了当前不支持的特性。",
            "invalid_schema": "结构化输出 Schema 配置无效。",
            "schema_validation_failed": "模型输出未通过本地 Schema 校验。",
            "structured_output_limit": "结构化输出或 Schema 超过本地校验资源上限。",
            "invalid_json": "模型未返回有效 JSON。",
            "invalid_response": "结构化输出必须是 JSON 对象。",
        }[code])


def _fail(code: str) -> NoReturn:
    raise StructuredOutputError(code)


@dataclass
class _Budget:
    remaining: int

    def spend(self, amount: int = 1) -> None:
        self.remaining -= amount
        if self.remaining < 0:
            _fail("structured_output_limit")


def _scan_json(value: Any, *, schema: bool) -> None:
    """Bound depth/nodes/text before serialization or recursive validation."""
    invalid = "invalid_schema" if schema else "invalid_json"
    nodes = _Budget(MAX_SCHEMA_NODES if schema else MAX_OUTPUT_NODES)
    text = _Budget(MAX_SCHEMA_BYTES if schema else MAX_OUTPUT_BYTES)
    max_depth = MAX_SCHEMA_DEPTH if schema else MAX_OUTPUT_DEPTH

    def visit(item: Any, depth: int) -> None:
        nodes.spend()
        if depth > max_depth:
            _fail("structured_output_limit")
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, str):
            if len(item) > text.remaining:
                _fail("structured_output_limit")
            try:
                text.spend(len(item.encode("utf-8")))
            except UnicodeError:
                _fail(invalid)
        elif isinstance(item, (int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                _fail(invalid)
        elif isinstance(item, list):
            if len(item) > nodes.remaining:
                _fail("structured_output_limit")
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, dict):
            if len(item) > nodes.remaining:
                _fail("structured_output_limit")
            for key, child in item.items():
                if not isinstance(key, str):
                    _fail(invalid)
                visit(key, depth + 1)
                visit(child, depth + 1)
        else:
            _fail(invalid)

    visit(value, 0)


def _json_text(value: Any, *, schema: bool) -> str:
    _scan_json(value, schema=schema)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        _fail("invalid_schema" if schema else "invalid_json")
    if len(encoded.encode("utf-8")) > (MAX_SCHEMA_BYTES if schema else MAX_OUTPUT_BYTES):
        _fail("structured_output_limit")
    return encoded


def prepare_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate and snapshot the supported subset before any async model call."""
    if schema is None:
        return None
    if not isinstance(schema, dict):
        _fail("invalid_schema")
    snapshot = json.loads(_json_text(schema, schema=True))

    def check(spec: Any) -> None:
        if isinstance(spec, bool):
            return
        if not isinstance(spec, dict):
            _fail("invalid_schema")
        if set(spec) - _KEYWORDS:
            _fail("unsupported_schema")
        if "type" in spec:
            types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
            if not types or any(not isinstance(item, str) or item not in _TYPES for item in types):
                _fail("invalid_schema")
            if len(set(types)) != len(types):
                _fail("invalid_schema")
        if "properties" in spec:
            if not isinstance(spec["properties"], dict):
                _fail("invalid_schema")
            for child in spec["properties"].values():
                check(child)
        for keyword in ("items", "additionalProperties"):
            if keyword in spec:
                check(spec[keyword])
        if "required" in spec:
            required = spec["required"]
            if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
                _fail("invalid_schema")
            if len(set(required)) != len(required):
                _fail("invalid_schema")
        for keyword in _LENGTHS & spec.keys():
            if type(spec[keyword]) is not int or spec[keyword] < 0:
                _fail("invalid_schema")
        for keyword in _NUMBERS & spec.keys():
            if type(spec[keyword]) not in (int, float):
                _fail("invalid_schema")
        if "enum" in spec and (not isinstance(spec["enum"], list) or not spec["enum"]):
            _fail("invalid_schema")
        for keyword in ("title", "description", "$comment"):
            if keyword in spec and not isinstance(spec[keyword], str):
                _fail("invalid_schema")
        if "examples" in spec and not isinstance(spec["examples"], list):
            _fail("invalid_schema")

    check(snapshot)
    return snapshot


def parse_output(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        if len(content) > MAX_OUTPUT_BYTES:
            _fail("structured_output_limit")
        try:
            if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
                _fail("structured_output_limit")
        except UnicodeError:
            _fail("invalid_json")

        def reject_constant(_: str) -> NoReturn:
            _fail("invalid_json")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    _fail("invalid_json")
                result[key] = value
            return result

        try:
            content = json.loads(content, parse_constant=reject_constant, object_pairs_hook=unique_object)
        except StructuredOutputError:
            raise
        except RecursionError:
            _fail("structured_output_limit")
        except (TypeError, ValueError):
            _fail("invalid_json")
    if not isinstance(content, dict):
        _fail("invalid_response")
    _json_text(content, schema=False)
    return content


def validate_output(value: dict[str, Any], schema: dict[str, Any] | None) -> None:
    if schema is None:
        return
    budget = _Budget(MAX_VALIDATION_STEPS)

    def equal(left: Any, right: Any) -> bool:
        budget.spend()
        if isinstance(left, bool) or isinstance(right, bool):
            return type(left) is type(right) and left == right
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            return left == right
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
        if isinstance(left, list):
            return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
        return left == right

    def matches_type(item: Any, name: str) -> bool:
        return {
            "object": isinstance(item, dict), "array": isinstance(item, list),
            "string": isinstance(item, str), "boolean": isinstance(item, bool),
            "null": item is None,
            "number": type(item) in (int, float),
            "integer": type(item) is int or (type(item) is float and item.is_integer()),
        }[name]

    def check(item: Any, spec: Any) -> None:
        budget.spend()
        if spec is False:
            _fail("schema_validation_failed")
        if spec is True:
            return
        if "type" in spec:
            types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
            if not any(matches_type(item, name) for name in types):
                _fail("schema_validation_failed")
        if "enum" in spec and not any(equal(item, option) for option in spec["enum"]):
            _fail("schema_validation_failed")
        if "const" in spec and not equal(item, spec["const"]):
            _fail("schema_validation_failed")
        if isinstance(item, dict):
            if any(key not in item for key in spec.get("required", [])):
                _fail("schema_validation_failed")
            properties = spec.get("properties", {})
            additional = spec.get("additionalProperties", True)
            for key, child in item.items():
                check(child, properties[key] if key in properties else additional)
        if isinstance(item, (str, list)):
            low, high = ("minLength", "maxLength") if isinstance(item, str) else ("minItems", "maxItems")
            if (low in spec and len(item) < spec[low]) or (high in spec and len(item) > spec[high]):
                _fail("schema_validation_failed")
        if isinstance(item, list) and "items" in spec:
            for child in item:
                check(child, spec["items"])
        if type(item) in (int, float):
            if (
                ("minimum" in spec and item < spec["minimum"])
                or ("maximum" in spec and item > spec["maximum"])
                or ("exclusiveMinimum" in spec and item <= spec["exclusiveMinimum"])
                or ("exclusiveMaximum" in spec and item >= spec["exclusiveMaximum"])
            ):
                _fail("schema_validation_failed")

    check(value, schema)
