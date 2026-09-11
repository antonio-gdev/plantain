"""Complete adversarial coverage for central redaction and scenario secret tracking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from plantain.security import redaction, secrets
from plantain.security.redaction import REDACTED, RedactionPolicy
from plantain.security.secrets import SecretRegistry

FIRST_OBSERVED = "synthetic-observed-value-one"
SECOND_OBSERVED = "synthetic-observed-value-two"
EXPECTED_REDACTION_COUNT = 2
JWT_VALUE = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnop"


@dataclass
class ExampleRecord:
    authorization: str
    label: str


class Dumpable:
    def model_dump(self, *, mode: str) -> dict[str, str]:
        assert mode == "python"
        return {"apiKey": FIRST_OBSERVED, "label": "visible"}


class Renderable:
    def __str__(self) -> str:
        return f"authorization={FIRST_OBSERVED}"


def test_policy_normalizes_configured_names_and_sensitive_references() -> None:
    policy = RedactionPolicy((" IP_ADDRESS ", "ip_address", "account-number", ""))

    assert policy.sensitive_keys == ("account-number", "ip_address")
    assert policy.is_sensitive_key("Authorization") is True
    assert policy.is_sensitive_key("CLIENT-SECRET") is True
    assert policy.is_sensitive_key("IP_ADDRESS") is True
    assert policy.is_sensitive_key("customer_ip_address") is False
    assert policy.contains_sensitive_data("captured Ip_AdDrEsS field") is True
    assert policy.contains_sensitive_data("ordinary diagnostic") is False
    assert policy.is_sensitive_reference("css=input[name='password']") is True
    assert policy.is_sensitive_reference("css=#ordinary") is False


@pytest.mark.parametrize(
    "value, expected",
    [
        (
            f"Bearer {FIRST_OBSERVED}",
            f"Bearer {REDACTED}",
        ),
        (
            f"basic {FIRST_OBSERVED}",
            f"basic {REDACTED}",
        ),
        (
            f"password: {FIRST_OBSERVED}",
            f"password={REDACTED}",
        ),
        (
            f"api_key={FIRST_OBSERVED}; next=value",
            f"api_key={REDACTED}; next=value",
        ),
        (
            f"opaque {JWT_VALUE}",
            f"opaque {REDACTED}",
        ),
    ],
)
def test_unstructured_credential_patterns_are_redacted(value: str, expected: str) -> None:
    assert RedactionPolicy().redact_text(value) == expected


def test_url_redaction_strips_userinfo_and_handles_query_and_fragments() -> None:
    policy = RedactionPolicy()
    value = (
        f"https://user:{FIRST_OBSERVED}@example.test:8443/path?"
        f"authorization={FIRST_OBSERVED}&view=summary#route="
        f"details&token={SECOND_OBSERVED}"
    )

    rendered = policy.redact_url(value)

    assert rendered.startswith("https://example.test:8443/path?")
    assert "user" not in rendered
    assert FIRST_OBSERVED not in rendered
    assert SECOND_OBSERVED not in rendered
    assert "view=summary" in rendered
    assert rendered.count("%2A%2A%2AREDACTED%2A%2A%2A") == EXPECTED_REDACTION_COUNT


def test_url_redaction_handles_spa_query_ipv6_and_malformed_values() -> None:
    policy = RedactionPolicy()

    ipv6 = policy.redact_url(f"https://[2001:db8::1]/app#/route?token={FIRST_OBSERVED}&tab=details")
    assert ipv6.startswith("https://[2001:db8::1]/app#/route?")
    assert FIRST_OBSERVED not in ipv6
    assert "tab=details" in ipv6
    assert policy.redact_url("https://example.test:invalid/path") == "<malformed-url>"
    assert policy.redact_url(f"password={FIRST_OBSERVED}") == f"password={REDACTED}"
    assert policy.redact_url("https://[invalid/path") == "https://[invalid/path"


def test_opaque_token_masking_preserves_ordinary_long_identifiers() -> None:
    digest = "a" * 64
    policy = RedactionPolicy()

    rendered = policy.redact_text(f"jwt={JWT_VALUE}; digest={digest}")
    assert JWT_VALUE not in rendered
    assert digest in rendered

    rendered_url = policy.redact_url(
        f"https://example.test/callback?state={JWT_VALUE}&digest={digest}"
    )
    assert JWT_VALUE not in rendered_url
    assert digest in rendered_url


def test_query_parse_failure_falls_back_to_text_redaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_query(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("synthetic query detail")

    monkeypatch.setattr(redaction, "parse_qsl", reject_query)
    rendered = RedactionPolicy().redact_url(f"https://example.test/path?token={FIRST_OBSERVED}")
    assert FIRST_OBSERVED not in rendered
    assert REDACTED in rendered


def test_redaction_supports_binary_dataclass_model_and_fallback_values() -> None:
    policy = RedactionPolicy()
    value = {
        "bytes": b"abc",
        "bytearray": bytearray(b"abcd"),
        "memory": memoryview(b"xy"),
        "record": ExampleRecord(authorization=FIRST_OBSERVED, label="visible"),
        "model": Dumpable(),
        "fallback": Renderable(),
    }

    rendered = policy.redact_artifact(value)

    assert rendered["bytes"] == "<bytes:3>"
    assert rendered["bytearray"] == "<bytes:4>"
    assert rendered["memory"] == "<bytes:2>"
    assert rendered["record"] == {"authorization": REDACTED, "label": "visible"}
    assert rendered["model"] == {"apiKey": REDACTED, "label": "visible"}
    assert rendered["fallback"] == f"authorization={REDACTED}"


def test_mapping_and_sequence_cycles_are_rendered_safely() -> None:
    mapping: dict[str, Any] = {}
    sequence: list[Any] = []
    mapping["self"] = mapping
    sequence.append(sequence)

    assert RedactionPolicy().redact_artifact(mapping) == {"self": "<cyclic-reference>"}
    assert RedactionPolicy().redact_artifact(sequence) == ["<cyclic-reference>"]


def test_log_redaction_bounds_mapping_items_and_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redaction, "MAX_LOG_REDACTED_ITEMS", 2)
    monkeypatch.setattr(redaction, "MAX_LOG_REDACTION_DEPTH", 1)
    policy = RedactionPolicy()

    mapping = policy.redact_log({"first": 1, "second": 2, "third": 3})
    nested = policy.redact_log({"first": {"second": {"third": "value"}}})

    assert mapping == {
        "first": 1,
        "second": 2,
        "<truncated>": "1 additional entries",
    }
    assert nested == {"first": {"second": "<maximum-log-redaction-depth>"}}


def test_artifact_redaction_depth_limit_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(redaction, "MAX_ARTIFACT_REDACTION_DEPTH", 2)
    nested = {"first": {"second": {"third": "visible"}}}

    with pytest.raises(RuntimeError, match="internal depth limit"):
        RedactionPolicy().redact_artifact(nested)


def test_public_redaction_functions_use_default_and_configured_policy() -> None:
    assert redaction.is_sensitive_key("password") is True
    assert redaction.is_sensitive_key("ip_address", sensitive_keys=("ip_address",)) is True
    assert (
        redaction.contains_sensitive_data(
            "contains ip_address",
            sensitive_keys=("ip_address",),
        )
        is True
    )
    assert redaction.redact_text(f"token={FIRST_OBSERVED}") == f"token={REDACTED}"
    assert FIRST_OBSERVED not in redaction.redact_url(
        f"https://example.test?token={FIRST_OBSERVED}"
    )
    assert redaction.redact({"password": FIRST_OBSERVED}) == {"password": REDACTED}
    assert redaction.redact_artifact(
        {"message": "ip_address"},
        sensitive_keys=("ip_address",),
    ) == {"message": REDACTED}


def test_secret_registry_observes_environment_and_nested_sensitive_values() -> None:
    registry = SecretRegistry(sensitive_keys=("account_number",))
    registry.observe_environment("ordinary", FIRST_OBSERVED)
    registry.observe_environment("account_number", FIRST_OBSERVED)
    registry.observe(
        {
            "password": [SECOND_OBSERVED, {"nested": SECOND_OBSERVED.encode("utf-8")}],
            "ordinary": "visible",
        }
    )

    rendered = registry.redact_text(f"first={FIRST_OBSERVED}; second={SECOND_OBSERVED}; visible")
    assert FIRST_OBSERVED not in rendered
    assert SECOND_OBSERVED not in rendered
    assert rendered.count(REDACTED) == EXPECTED_REDACTION_COUNT
    assert "visible" in rendered


def test_secret_registry_handles_dumpable_cycles_and_invalid_utf8() -> None:
    registry = SecretRegistry()
    cyclic: dict[str, Any] = {"password": FIRST_OBSERVED}
    cyclic["self"] = cyclic
    registry.observe(cyclic)
    registry.observe({"password": b"\xff"})
    registry.observe(Dumpable(), _sensitive=True)

    assert registry.redact_text(FIRST_OBSERVED) == REDACTED
    assert registry.redact_text(SECOND_OBSERVED) == SECOND_OBSERVED


def test_secret_registry_log_field_multiline_and_clear() -> None:
    registry = SecretRegistry()
    registry.observe({"password": FIRST_OBSERVED})

    assert registry.redact_log_field("css=#password", "visible") == REDACTED
    assert registry.redact_log_field("css=#label", FIRST_OBSERVED) == REDACTED
    multiline = registry.redact_multiline(f"first {FIRST_OBSERVED}\r\nsecond visible\n")
    assert multiline == f"first {REDACTED}\r\nsecond visible\n"
    assert FIRST_OBSERVED not in registry.redact_url(f"https://example.test/path/{FIRST_OBSERVED}")

    registry.clear()
    assert registry.redact_text(FIRST_OBSERVED) == FIRST_OBSERVED


def test_secret_registry_matcher_rebuilds_for_longest_values() -> None:
    registry = SecretRegistry()
    registry.observe({"password": "synthetic-value"})
    assert registry.redact_text("synthetic-value") == REDACTED
    registry.observe({"password": "synthetic-value-extended"})

    rendered = registry.redact_text("synthetic-value-extended synthetic-value")
    assert rendered == f"{REDACTED} {REDACTED}"


def test_secret_registry_redacts_short_values_without_masking_words() -> None:
    registry = SecretRegistry()
    for value in ("x", "ab", REDACTED, FIRST_OBSERVED, FIRST_OBSERVED):
        registry.observe({"password": value})

    rendered = registry.redact_text("x /ab/ example text")
    assert rendered == f"{REDACTED} /{REDACTED}/ example text"
    assert registry.redact_text("ab") == REDACTED
    assert registry.redact_text(REDACTED) == REDACTED
    assert registry.redact_text(FIRST_OBSERVED) == REDACTED


def test_secret_registry_detects_observed_values_without_exposing_them() -> None:
    registry = SecretRegistry()
    assert registry.contains_observed_text(FIRST_OBSERVED) is False

    registry.observe({"password": FIRST_OBSERVED})
    registry.observe({"password": "ab"})

    assert registry.contains_observed_text(f"prefix {FIRST_OBSERVED}") is True
    assert registry.contains_observed_text("/ab/") is True
    assert registry.contains_observed_text("label") is False

    registry.clear()
    assert registry.contains_observed_text(FIRST_OBSERVED) is False


def test_secret_registry_capacity_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets, "MAX_OBSERVED_SECRET_VALUES", 1)
    registry = SecretRegistry()
    registry.observe({"password": FIRST_OBSERVED})

    with pytest.raises(RuntimeError, match="fail-closed capacity"):
        registry.observe({"password": SECOND_OBSERVED})


def test_secret_registry_total_byte_capacity_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        secrets,
        "MAX_OBSERVED_SECRET_BYTES",
        len(FIRST_OBSERVED.encode("utf-8")),
    )
    registry = SecretRegistry()
    registry.observe({"password": FIRST_OBSERVED})

    with pytest.raises(RuntimeError, match="fail-closed capacity"):
        registry.observe({"password": SECOND_OBSERVED})
