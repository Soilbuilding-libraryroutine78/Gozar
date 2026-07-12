"""Tests for Gozar-only SDK routing controls."""

from gozar.gateway.upstream import to_json_body
from gozar.translation.types import OpenAIChatRequest


def test_gozar_extra_body_is_consumed_before_provider_passthrough() -> None:
    request = OpenAIChatRequest.model_validate(
        {
            "model": "route-input",
            "messages": [{"role": "user", "content": "hello"}],
            "gozar": {
                "chain_id": "11111111-1111-4111-8111-111111111111",
                "include_metadata": True,
            },
            "response_format": {"type": "json_object"},
        }
    )

    provider_body = to_json_body(request)

    assert "gozar" not in provider_body
    assert provider_body["response_format"] == {"type": "json_object"}
