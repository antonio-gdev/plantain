"""Behavioral parity and security tests for structured redaction."""

from __future__ import annotations

from plantain.security.redaction import REDACTED, RedactionPolicy
from plantain.security.secrets import SecretRegistry

EXPECTED_BOUNDED_LOG_ITEMS = 201


def test_configured_structured_keys_use_exact_case_insensitive_matching() -> None:
    policy = RedactionPolicy(("ip_address",))

    result = policy.redact_artifact(
        {
            "IP_ADDRESS": "10.0.0.1",
            "customer_ip_address": "remains-visible",
        }
    )

    assert result == {
        "IP_ADDRESS": REDACTED,
        "customer_ip_address": "remains-visible",
    }


def test_configured_key_fragments_mask_whole_strings_at_every_nested_depth() -> None:
    policy = RedactionPolicy(("ip_address",))

    result = policy.redact_artifact(
        {
            "outer": [
                {"message": "Observed IP_ADDRESS in payload"},
                ["free-form ip_address text"],
            ]
        }
    )

    assert result == {"outer": [{"message": REDACTED}, [REDACTED]]}


def test_artifact_redaction_is_complete_while_log_redaction_is_bounded() -> None:
    policy = RedactionPolicy()
    source = list(range(250))

    artifact = policy.redact_artifact(source)
    log_value = policy.redact_log(source)

    assert artifact == source
    assert len(log_value) == EXPECTED_BOUNDED_LOG_ITEMS
    assert log_value[-1] == "<50 additional entries>"


def test_observed_sensitive_values_remain_masked_after_context_chaining() -> None:
    registry = SecretRegistry(sensitive_keys=("first_password",))
    registry.observe({"first_password": "example-sensitive-value"})

    result = registry.redact({"copied": "prefix example-sensitive-value suffix"})

    assert result == {"copied": f"prefix {REDACTED} suffix"}


def test_url_redaction_preserves_spa_route_and_masks_sensitive_parameters() -> None:
    safe_url = RedactionPolicy().redact_url(
        "https://example.test/app#/orders?token=example-token&tab=details"
    )

    assert safe_url.startswith("https://example.test/app#/orders?")
    assert "example-token" not in safe_url
    assert "tab=details" in safe_url


def test_configured_fragment_masks_an_entire_url() -> None:
    policy = RedactionPolicy(("ip_address",))

    assert policy.redact_url("https://example.test/ip_address") == REDACTED
