"""Slug validation: format rules and normalisation."""
from __future__ import annotations

import pytest

from server.config import SlugError, validate_slug


class TestSlugFormat:
    @pytest.mark.parametrize(
        "good",
        [
            "ab",  # min length
            "acme",
            "acme-pitch",
            "client-2026-pitch",
            "a1",
            "a" * 30,  # max length
            "lupker-makelaardij",
        ],
    )
    def test_accepts_valid(self, good: str) -> None:
        assert validate_slug(good) == good

    def test_normalises_whitespace_and_case(self) -> None:
        assert validate_slug("  Acme-Pitch  ") == "acme-pitch"

    @pytest.mark.parametrize(
        ("bad", "needle"),
        [
            ("", "empty"),
            ("a", "too short"),
            ("a" * 31, "too long"),
            ("-acme", "invalid"),
            ("acme-", "invalid"),
            ("acme.pitch", "invalid"),
            ("acme_pitch", "invalid"),
            ("acme/pitch", "invalid"),
            ("acme pitch", "invalid"),
            ("acme--pitch", None),  # double hyphens are allowed by current regex
        ],
    )
    def test_rejects_or_accepts(self, bad: str, needle: str | None) -> None:
        if needle is None:
            # Sanity: this case is meant to PASS — caught here so we're explicit
            # if behaviour ever changes.
            assert validate_slug(bad) == bad
        else:
            with pytest.raises(SlugError) as excinfo:
                validate_slug(bad)
            assert needle.lower() in str(excinfo.value).lower()

    def test_rejects_non_string(self) -> None:
        with pytest.raises(SlugError):
            validate_slug(123)  # type: ignore[arg-type]


class TestUppercaseHandling:
    def test_uppercase_is_lowercased_before_validation(self) -> None:
        # Pure uppercase that's otherwise valid should be normalised, not rejected.
        assert validate_slug("ACME") == "acme"

    def test_uppercase_with_invalid_chars_still_fails(self) -> None:
        with pytest.raises(SlugError):
            validate_slug("ACME_PITCH")
