from __future__ import annotations

import pytest

from llm_gateway.projects.keys import decode_pepper, issue_key, parse_key, secret_matches
from tests.conftest import TEST_PEPPER

PEPPER = decode_pepper(TEST_PEPPER)


def test_issued_key_parses_back_and_matches_its_hash() -> None:
    key = issue_key("production", PEPPER)

    presented = parse_key(key.raw)

    assert key.raw.startswith(f"lgw.prod.{key.key_id}.")
    assert presented is not None
    assert (presented.key_id, presented.environment) == (key.key_id, "production")
    assert secret_matches(PEPPER, presented.secret, key.secret_hmac)
    assert presented.secret not in key.secret_hmac


def test_development_key_has_the_dev_prefix() -> None:
    assert issue_key("development", PEPPER).raw.startswith("lgw.dev.")


def test_keys_are_unique() -> None:
    assert len({issue_key("production", PEPPER).raw for _ in range(200)}) == 200


@pytest.mark.parametrize(
    "raw",
    [
        "sk-abc123",
        "",
        "lgw.test.abcdefghijkl." + "a" * 43,
        "lgw.prod.ABCDEFGHIJKL." + "a" * 43,
        "lgw.prod.abcdefghijkl." + "a" * 42,
        "lgw.prod.abcdefghijkl." + "a" * 43 + "x",
        "lgw.prod.abcdefghijkl." + "a" * 42 + "=",
    ],
)
def test_malformed_keys_do_not_parse(raw: str) -> None:
    assert parse_key(raw) is None


def test_wrong_secret_or_pepper_does_not_match() -> None:
    key = issue_key("production", PEPPER)
    presented = parse_key(key.raw)
    assert presented is not None

    assert not secret_matches(PEPPER, "x" * 43, key.secret_hmac)
    assert not secret_matches(
        b"another-pepper-of-32-bytes-long!", presented.secret, key.secret_hmac
    )


@pytest.mark.parametrize("encoded", ["c2hvcnQ", "not base64 !!"])
def test_weak_or_invalid_pepper_is_rejected(encoded: str) -> None:
    with pytest.raises(ValueError, match="pepper"):
        decode_pepper(encoded)


def test_money_formatting() -> None:
    from decimal import Decimal

    from llm_gateway.money import format_usd, to_micro_usd

    assert to_micro_usd(Decimal("3.50")) == 3_500_000
    assert [format_usd(v) for v in (0, 1_500_000, 125, 5_000_000)] == [
        "0.00",
        "1.50",
        "0.000125",
        "5.00",
    ]
