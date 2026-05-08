"""Builder mode detection + build orchestration.

We do *not* run a real ``pnpm install`` in CI — that's too slow and
flaky. For the node-based modes we monkeypatch ``server.builder._run``
to act as if pnpm ran successfully and then materialise the output dir
ourselves so the resolver has something to find.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import builder
from server.builder import (
    BUILD_OUTPUT_CANDIDATES,
    BuildError,
    BuildResult,
    _resolve_build_output,
    build,
    detect_mode,
)


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

    def test_detect_vite_via_devdep(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"devDependencies": {"vite": "^5.0.0"}}),
        )
        assert detect_mode(tmp_path) == "vite"

    def test_detect_vite_via_dependency(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"dependencies": {"vite": "^5.0.0"}}),
        )
        assert detect_mode(tmp_path) == "vite"

    @pytest.mark.parametrize(
        "config_name",
        ["vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs"],
    )
    def test_detect_vite_via_config_file(
        self, tmp_path: Path, config_name: str
    ) -> None:
        _write(tmp_path / "package.json", json.dumps({}))
        _write(tmp_path / config_name, "export default {}")
        assert detect_mode(tmp_path) == "vite"

    def test_detect_vite_via_config_file_without_package_json(
        self, tmp_path: Path
    ) -> None:
        # Edge case: vite.config.ts present but no package.json.
        _write(tmp_path / "vite.config.ts", "export default {}")
        assert detect_mode(tmp_path) == "vite"

    def test_both_next_and_vite_raises(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps(
                {
                    "dependencies": {"next": "^14.0.0"},
                    "devDependencies": {"vite": "^5.0.0"},
                }
            ),
        )
        with pytest.raises(BuildError) as exc:
            detect_mode(tmp_path)
        msg = str(exc.value).lower()
        assert "both" in msg
        assert "next" in msg and "vite" in msg

    def test_package_json_without_next_or_vite_raises(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"dependencies": {"react": "^18.0.0"}}),
        )
        with pytest.raises(BuildError) as exc:
            detect_mode(tmp_path)
        msg = str(exc.value).lower()
        assert "next" in msg
        assert "vite" in msg

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


class TestResolveBuildOutput:
    """Output-dir resolution: dist > out > build, first existing wins."""

    @pytest.mark.parametrize("name", BUILD_OUTPUT_CANDIDATES)
    def test_each_candidate_alone(self, tmp_path: Path, name: str) -> None:
        (tmp_path / name).mkdir()
        assert _resolve_build_output(tmp_path) == tmp_path / name

    def test_dist_wins_over_out(self, tmp_path: Path) -> None:
        (tmp_path / "dist").mkdir()
        (tmp_path / "out").mkdir()
        assert _resolve_build_output(tmp_path) == tmp_path / "dist"

    def test_dist_wins_over_build(self, tmp_path: Path) -> None:
        (tmp_path / "dist").mkdir()
        (tmp_path / "build").mkdir()
        assert _resolve_build_output(tmp_path) == tmp_path / "dist"

    def test_out_wins_over_build_when_no_dist(self, tmp_path: Path) -> None:
        (tmp_path / "out").mkdir()
        (tmp_path / "build").mkdir()
        assert _resolve_build_output(tmp_path) == tmp_path / "out"

    def test_returns_none_when_no_candidate_present(self, tmp_path: Path) -> None:
        assert _resolve_build_output(tmp_path) is None

    def test_files_named_dist_etc_do_not_count(self, tmp_path: Path) -> None:
        # a regular file called "dist" must not be picked up
        (tmp_path / "dist").write_text("not a directory")
        assert _resolve_build_output(tmp_path) is None


class TestBuildNode:
    """Build orchestration with the subprocess mocked.

    The fake ``_run`` materialises the build output directory we tell it
    to, so the orchestration code thinks pnpm ran fine. This isolates
    what we own (mode → command sequence → output resolution) from what
    we don't (pnpm itself).
    """

    @staticmethod
    def _patch_run(monkeypatch, output_dir_name: str | None) -> list[list[str]]:
        calls: list[list[str]] = []

        def fake_run(cmd, cwd, timeout_s=300):
            calls.append(list(cmd))
            # On the second call (`pnpm run build`), create the output dir.
            # Identifying by argv is more robust than by call index.
            if "build" in cmd and output_dir_name is not None:
                (Path(cwd) / output_dir_name / "index.html").parent.mkdir(
                    parents=True, exist_ok=True
                )
                (Path(cwd) / output_dir_name / "index.html").write_text("hi")
            return f"<fake output for {' '.join(cmd)}>\n"

        monkeypatch.setattr(builder, "_run", fake_run)
        # Pretend pnpm is installed.
        monkeypatch.setattr(builder.shutil, "which", lambda _: "/usr/bin/pnpm")
        return calls

    def test_vite_build_produces_dist(self, tmp_path: Path, monkeypatch) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"devDependencies": {"vite": "^5.0.0"}}),
        )
        calls = self._patch_run(monkeypatch, output_dir_name="dist")

        result = build(tmp_path)
        assert result.mode == "vite"
        assert result.output_dir == tmp_path / "dist"
        assert (result.output_dir / "index.html").read_text() == "hi"
        # Two pnpm invocations: install then run build.
        assert len(calls) == 2
        assert calls[0][1] == "install"
        assert calls[1][:2] == ["/usr/bin/pnpm", "run"]

    def test_next_build_produces_out(self, tmp_path: Path, monkeypatch) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"dependencies": {"next": "^14.0.0"}}),
        )
        self._patch_run(monkeypatch, output_dir_name="out")

        result = build(tmp_path)
        assert result.mode == "next"
        assert result.output_dir == tmp_path / "out"

    def test_build_output_missing_returns_clear_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"devDependencies": {"vite": "^5.0.0"}}),
        )
        # output_dir_name=None → the fake build doesn't create any output dir
        self._patch_run(monkeypatch, output_dir_name=None)

        with pytest.raises(BuildError) as exc:
            build(tmp_path)
        msg = str(exc.value).lower()
        # The error message must list every candidate by name so the user
        # can see what we looked for.
        for name in BUILD_OUTPUT_CANDIDATES:
            assert name in msg
        # And must include the captured pnpm output for debugging.
        assert "pnpm install" in (exc.value.logs or "")
        assert "pnpm run build" in (exc.value.logs or "")

    def test_pnpm_not_installed_raises(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _write(
            tmp_path / "package.json",
            json.dumps({"devDependencies": {"vite": "^5.0.0"}}),
        )
        monkeypatch.setattr(builder.shutil, "which", lambda _: None)
        with pytest.raises(BuildError) as exc:
            build(tmp_path)
        assert "pnpm is not installed" in str(exc.value).lower()
