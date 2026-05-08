"""Reserved-slug blocklist: every name is rejected with a clear reason."""
from __future__ import annotations

import pytest

from server.config import RESERVED_SLUGS, SlugError, validate_slug

# Every reserved name from the brief must be in the blocklist.
EXPECTED_RESERVED = {
    "sales",
    "deploy",
    "trades",
    "stax",
    "api",
    "diwan",
    "diwantravel",
    "forge",
    "www",
    "mail",
    "smtp",
    "imap",
    "pop",
    "ftp",
    "ns1",
    "ns2",
    "cpanel",
    "webmail",
    "admin",
    "root",
}


def test_blocklist_contains_every_required_name() -> None:
    missing = EXPECTED_RESERVED - RESERVED_SLUGS
    assert not missing, f"blocklist is missing required reserved names: {missing}"


@pytest.mark.parametrize("name", sorted(EXPECTED_RESERVED))
def test_reserved_names_are_rejected(name: str) -> None:
    with pytest.raises(SlugError) as excinfo:
        validate_slug(name)
    msg = str(excinfo.value).lower()
    assert "reserved" in msg
    assert name in msg, f"error message should name the offending slug: {msg!r}"


def test_reserved_names_are_rejected_after_normalisation() -> None:
    # Mixed-case input still hits the blocklist post-lowercase.
    with pytest.raises(SlugError):
        validate_slug("  SALES  ")


def test_non_reserved_names_pass() -> None:
    # Sanity: a similar-but-not-reserved name should be allowed through.
    assert validate_slug("salesy") == "salesy"
    assert validate_slug("forge-test") == "forge-test"
