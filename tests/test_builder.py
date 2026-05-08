"""Builder mode detection + static demo passthrough.

We do *not* run a real ``pnpm install`` in CI — that's too slow and
flaky. Instead we test:

* mode detection across the three relevant cases
* static-mode build() returns the staging dir unchanged
* a malformed package.json raises a clean BuildError
* a package.json without 'next' raises a clean BuildError
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.builder import BuildError, BuildResult, build, detect_mode


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


class TestDetectMode:
    def test_no_package_json_means_static(self, tmp_path: Path) -> None:
        _write(tmp_path / "index.html", "<!doctype html>hello")
        assert detect_mode(tmp_path) == "static"

    def test_package_json_with_next_means_next(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"dependencies": {"next": "^14.0.0"}}),
        )
        assert detect_mode(tmp_path) == "next"

    def test_package_json_with_next_in_devDependencies_means_next(
        self, tmp_path: Path
    ) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"devDependencies": {"next": "^14.0.0"}}),
        )
        assert detect_mode(tmp_path) == "next"

    def test_package_json_without_next_raises(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"dependencies": {"react": "^18.0.0"}}),
        )
        with pytest.raises(BuildError) as exc:
            detect_mode(tmp_path)
        assert "next" in str(exc.value).lower()

    def test_malformed_package_json_raises(self, tmp_path: Path) -> None:
        _write(tmp_path / "package.json", "{not json")
        with pytest.raises(BuildError) as exc:
            detect_mode(tmp_path)
        assert "valid json" in str(exc.value).lower()


class TestBuildStatic:
    def test_static_build_returns_staging_dir(self, tmp_path: Path) -> None:
        _write(tmp_path / "index.html", "<!doctype html>hi")
        _write(tmp_path / "styles/app.css", "body{}")
        result = build(tmp_path)
        assert isinstance(result, BuildResult)
        assert result.mode == "static"
        assert result.output_dir == tmp_path
        assert "no build step" in result.logs.lower()
