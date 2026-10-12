"""Caddy snippet rendering, atomic write, flock, reload-and-rollback."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from server.caddy import (
    CaddyError,
    _strip_sudo_prefix,
    caddy_lock,
    check_reload_permission,
    install_snippet,
    remove_snippet,
    render_snippet,
    snippet_path,
)

DEMOS = Path("/var/www/demos")


class TestRenderSnippet:
    def test_includes_slug_and_root(self) -> None:
        out = render_snippet("acme-pitch", DEMOS)
        assert "acme-pitch.baboons.dev {" in out
        assert "root * /var/www/demos/acme-pitch" in out

    def test_includes_static_serving_directives(self) -> None:
        out = render_snippet("acme", DEMOS)
        # The full SPA-aware try_files chain. Order matters — {path} first
        # so genuine assets win, /index.html last so SPA deep links land
        # on the entry point and the client-side router takes over.
        assert "try_files {path} {path}/index.html /index.html" in out
        assert "file_server" in out
        assert "encode zstd gzip" in out

    def test_try_files_includes_spa_fallback(self) -> None:
        """SPA frameworks (React Router, Vue Router, SvelteKit, Vite-+-React)
        depend on every unknown path falling back to /index.html so their
        client-side router can render the right view.

        Without ``/index.html`` in try_files, deep links like
        ``/about/team`` 404 and the demo only works at the root URL —
        which we hit on the dekkers and lupker-makelaardij planning.
        """
        out = render_snippet("acme", DEMOS)
        # Pull just the try_files line and check the final fallback.
        try_files_line = next(
            line for line in out.splitlines() if "try_files" in line
        )
        tokens = try_files_line.split()
        # Format: ``try_files {path} {path}/index.html /index.html``
        assert tokens[-1] == "/index.html", (
            f"final try_files fallback must be /index.html (the SPA "
            f"fallback) — got {tokens[-1]!r} from line {try_files_line!r}"
        )

    def test_includes_cache_headers_matching_stax_pattern(self) -> None:
        out = render_snippet("acme", DEMOS)
        # immutable cache for /assets/* and /images/*
        assert "@assets path /assets/* /images/*" in out
        assert 'Cache-Control "public, max-age=31536000, immutable"' in out
        # no-cache on the HTML entry point
        assert "@html path /index.html /" in out
        assert 'Cache-Control "no-cache, must-revalidate"' in out

    def test_imports_per_slug_extras_before_spa_fallback(self) -> None:
        """Hand-managed routes in ``extras/<slug>.caddy`` must run before
        try_files, or the SPA fallback rewrites e.g. /data/* to /index.html."""
        out = render_snippet("acme", DEMOS, Path("/etc/caddy/sites.d/extras"))
        assert "import /etc/caddy/sites.d/extras/acme.caddy*" in out
        lines = [line.strip() for line in out.splitlines()]
        assert lines.index("route {") < lines.index("import /etc/caddy/sites.d/extras/acme.caddy*")
        assert lines.index("import /etc/caddy/sites.d/extras/acme.caddy*") < next(
            i for i, line in enumerate(lines) if line.startswith("try_files")
        )

    def test_render_uses_custom_demos_root(self) -> None:
        out = render_snippet("foo", Path("/tmp/demos"))
        assert "root * /tmp/demos/foo" in out


class TestInstallSnippet:
    def test_writes_then_reload_succeeds(self, tmp_path: Path) -> None:
        sites = tmp_path / "sites.d"
        install_snippet("acme", sites, DEMOS, reload_cmd="true")
        p = snippet_path("acme", sites)
        assert p.exists()
        assert "acme.baboons.dev" in p.read_text()

    def test_rollback_when_brand_new_and_reload_fails(self, tmp_path: Path) -> None:
        sites = tmp_path / "sites.d"
        with pytest.raises(CaddyError):
            install_snippet("acme", sites, DEMOS, reload_cmd="false")
        # Brand-new snippet should have been deleted on rollback.
        assert not snippet_path("acme", sites).exists()

    def test_rollback_restores_previous_when_reload_fails(self, tmp_path: Path) -> None:
        sites = tmp_path / "sites.d"
        sites.mkdir()
        p = snippet_path("acme", sites)
        p.write_text("PREVIOUS CONTENT", encoding="utf-8")
        with pytest.raises(CaddyError):
            install_snippet("acme", sites, DEMOS, reload_cmd="false")
        assert p.read_text() == "PREVIOUS CONTENT"

    def test_caddy_error_carries_output(self, tmp_path: Path) -> None:
        sites = tmp_path / "sites.d"
        with pytest.raises(CaddyError) as exc:
            install_snippet(
                "acme",
                sites,
                DEMOS,
                reload_cmd="bash -c 'echo boom; exit 7'",
            )
        assert "boom" in exc.value.output
        assert "exit 7" in str(exc.value)


class TestRemoveSnippet:
    def test_removes_existing_snippet(self, tmp_path: Path) -> None:
        sites = tmp_path / "sites.d"
        sites.mkdir()
        p = snippet_path("acme", sites)
        p.write_text("old content", encoding="utf-8")
        removed = remove_snippet("acme", sites, "true")
        assert removed is True
        assert not p.exists()

    def test_returns_false_when_no_snippet_and_does_not_reload(
        self, tmp_path: Path
    ) -> None:
        sites = tmp_path / "sites.d"
        # reload_cmd would fail if it ran — proving we never invoked it
        removed = remove_snippet("ghost", sites, "false")
        assert removed is False

    def test_rollback_restores_file_on_reload_failure(self, tmp_path: Path) -> None:
        sites = tmp_path / "sites.d"
        sites.mkdir()
        p = snippet_path("acme", sites)
        p.write_text("must-survive", encoding="utf-8")
        with pytest.raises(CaddyError):
            remove_snippet("acme", sites, "false")
        assert p.exists()
        assert p.read_text() == "must-survive"


class TestSudoPreflight:
    @pytest.mark.parametrize(
        ("cmd", "expected"),
        [
            (
                "sudo -n /usr/bin/systemctl reload caddy",
                ["/usr/bin/systemctl", "reload", "caddy"],
            ),
            ("sudo /usr/bin/systemctl reload caddy", ["/usr/bin/systemctl", "reload", "caddy"]),
            ("sudo -E -n /bin/echo hi", ["/bin/echo", "hi"]),
            ("/usr/bin/systemctl reload caddy", None),  # no sudo prefix
            ("", None),  # empty
            ("sudo", None),  # sudo with nothing after
            ("sudo -n", None),  # sudo with only flags
        ],
    )
    def test_strip_sudo_prefix(self, cmd: str, expected) -> None:
        assert _strip_sudo_prefix(cmd) == expected

    def test_skips_when_no_sudo(self) -> None:
        ok, msg = check_reload_permission("/bin/true")
        assert ok is True
        assert "skipping preflight" in msg

    def test_real_sudo_check_returns_a_message(self) -> None:
        """The actual subprocess invocation. We can't predict whether the test
        runner has sudo permission for systemctl reload caddy, but we can
        confirm the function returns cleanly with a message either way.
        """
        ok, msg = check_reload_permission("sudo -n /usr/bin/systemctl reload caddy")
        assert isinstance(ok, bool)
        assert isinstance(msg, str)
        assert msg  # non-empty


class TestCaddyLock:
    def test_two_threads_mutually_exclude(self, tmp_path: Path) -> None:
        """If the lock is broken, both threads enter the critical section.

        We record (event, tag) pairs and assert every 'enter' is followed
        by its own 'exit' — i.e. no interleaving.
        """
        events: list[tuple[str, str]] = []
        lock_path = tmp_path / "forge-caddy.lock"
        barrier = threading.Barrier(2)

        def worker(tag: str) -> None:
            barrier.wait()
            with caddy_lock(lock_path):
                events.append(("enter", tag))
                time.sleep(0.05)  # widen the critical section so a leak is detectable
                events.append(("exit", tag))

        t1 = threading.Thread(target=worker, args=("a",))
        t2 = threading.Thread(target=worker, args=("b",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert len(events) == 4
        for i in range(0, len(events), 2):
            kind_in, tag_in = events[i]
            kind_out, tag_out = events[i + 1]
            assert kind_in == "enter"
            assert kind_out == "exit"
            assert tag_in == tag_out, f"interleaved: {events!r}"
