"""Tests for the embedded SSH SOCKS5 tunnel in tunnel.py.

No real ssh is ever spawned: ``subprocess.Popen`` and the port probe are
monkeypatched, so start/stop are driven by an in-memory ``FakeProc``.
"""

from __future__ import annotations

import io
import socket

import pytest

from opensearch_dashboard_mcp import tunnel as tunnel_mod
from opensearch_dashboard_mcp.tunnel import SshSocksTunnel, _pick_free_port, _port_open


class FakeProc:
    """Minimal subprocess.Popen stand-in with controllable liveness."""

    def __init__(self, *, alive_poll, returncode: int = 0, stderr: str = "") -> None:
        # alive_poll: what poll() returns while "running" (None = alive).
        self._alive_poll = alive_poll
        self.returncode = returncode
        self.stderr = io.StringIO(stderr)
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        if self.terminated or self.killed:
            return self.returncode
        return self._alive_poll

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout=None) -> int:
        self.waited = True
        return self.returncode

    def kill(self) -> None:
        self.killed = True


@pytest.fixture(autouse=True)
def _no_atexit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from piling stop() callbacks onto the real atexit registry."""
    monkeypatch.setattr(tunnel_mod.atexit, "register", lambda *a, **k: None)


class TestPureHelpers:
    def test_proxy_url_uses_socks5h_scheme(self) -> None:
        t = SshSocksTunnel("user@host", port=1080)
        assert t.proxy_url == "socks5h://127.0.0.1:1080"

    def test_build_cmd_contains_expected_flags(self) -> None:
        t = SshSocksTunnel("user@host", port=1080)
        cmd = t._build_cmd()
        assert cmd[0] == "ssh"
        assert "-N" in cmd
        assert "-D" in cmd
        assert "127.0.0.1:1080" in cmd
        assert cmd[-1] == "user@host"
        assert "BatchMode=yes" in cmd
        assert "ExitOnForwardFailure=yes" in cmd

    def test_pick_free_port_is_bindable_int(self) -> None:
        port = _pick_free_port()
        assert isinstance(port, int)
        # Nothing holds it, so it can be bound right now.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))

    def test_auto_picks_port_when_none(self) -> None:
        t = SshSocksTunnel("user@host")
        assert isinstance(t.port, int)
        assert t.port > 0

    def test_port_open_true_for_listening_socket(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            assert _port_open(port) is True

    def test_port_open_false_for_dead_port(self) -> None:
        port = _pick_free_port()  # free, nothing listening
        assert _port_open(port) is False


class TestStartStop:
    def test_start_returns_once_port_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = FakeProc(alive_poll=None)
        monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(tunnel_mod, "_port_open", lambda port: True)

        t = SshSocksTunnel("user@host", port=1080)
        t.start()
        assert t._proc is proc

    def test_start_polls_until_port_opens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = FakeProc(alive_poll=None)
        monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(tunnel_mod.time, "sleep", lambda s: None)  # no real wait
        probes = {"n": 0}

        def port_open(port):
            probes["n"] += 1
            return probes["n"] >= 2  # closed on first poll, open on the second

        monkeypatch.setattr(tunnel_mod, "_port_open", port_open)

        t = SshSocksTunnel("user@host", port=1080)
        t.start()
        assert probes["n"] >= 2  # looped at least once before the port came up
        assert t._proc is proc

    def test_start_raises_when_ssh_exits_early(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = FakeProc(alive_poll=1, returncode=1, stderr="Permission denied")
        monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(tunnel_mod, "_port_open", lambda port: False)

        t = SshSocksTunnel("user@host", port=1080)
        with pytest.raises(RuntimeError) as exc:
            t.start()
        assert "exited early" in str(exc.value)
        assert "Permission denied" in str(exc.value)

    def test_start_raises_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        proc = FakeProc(alive_poll=None)
        monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(tunnel_mod, "_port_open", lambda port: False)

        # connect_timeout=0 -> deadline already passed, loop never sleeps.
        t = SshSocksTunnel("user@host", port=1080, connect_timeout=0.0)
        with pytest.raises(RuntimeError, match="not ready within"):
            t.start()
        # Timed-out start still tears the child down.
        assert proc.terminated is True

    def test_stop_terminates_running_proc(self) -> None:
        proc = FakeProc(alive_poll=None)
        t = SshSocksTunnel("user@host", port=1080)
        t._proc = proc
        t.stop()
        assert proc.terminated is True
        assert proc.waited is True
        assert t._proc is None

    def test_stop_kills_when_terminate_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # poll stays None even after terminate -> escalate to kill().
        class Stubborn(FakeProc):
            def poll(self):
                return None  # never reports exit

        proc = Stubborn(alive_poll=None)
        t = SshSocksTunnel("user@host", port=1080)
        t._proc = proc
        t.stop()
        assert proc.terminated is True
        assert proc.killed is True

    def test_stop_is_noop_without_proc(self) -> None:
        t = SshSocksTunnel("user@host", port=1080)
        t.stop()  # no _proc -> must not raise
        assert t._proc is None

    def test_context_manager_starts_and_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = FakeProc(alive_poll=None)
        monkeypatch.setattr(tunnel_mod.subprocess, "Popen", lambda *a, **k: proc)
        monkeypatch.setattr(tunnel_mod, "_port_open", lambda port: True)

        with SshSocksTunnel("user@host", port=1080) as t:
            assert t._proc is proc
        # __exit__ terminated the child and cleared the handle.
        assert proc.terminated is True
        assert t._proc is None
