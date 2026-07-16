"""Optional embedded SSH SOCKS5 tunnel.

When ``SSH_PROXY_TO`` is set, the server spawns ``ssh -N -D <port> <target>`` and
routes httpx through the resulting SOCKS5 proxy. This lets the MCP server run on
the host while its requests egress from a remote host's network.

Everything here logs to **stderr**: stdout is the MCP stdio (JSON-RPC) channel and
must never be polluted.
"""

from __future__ import annotations

import atexit
import contextlib
import socket
import subprocess
import sys
import time


def _pick_free_port() -> int:
    """Ask the OS for an unused loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int) -> bool:
    """True once something accepts connections on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _log(msg: str) -> None:
    print(f"[ssh-tunnel] {msg}", file=sys.stderr, flush=True)


class SshSocksTunnel:
    """A managed ``ssh -N -D`` dynamic (SOCKS5) forward.

    Lifecycle mirrors the server: ``start()`` before serving, ``stop()`` after.
    ``proxy_url`` uses the ``socks5h`` scheme so DNS is resolved on the *remote*
    side (the tunnel endpoint), which is required when the target hostname only
    resolves through the remote network.
    """

    def __init__(
        self, target: str, port: int | None = None, *, connect_timeout: float = 15.0
    ) -> None:
        self.target = target
        self.port = port or _pick_free_port()
        self._connect_timeout = connect_timeout
        self._proc: subprocess.Popen[str] | None = None

    @property
    def proxy_url(self) -> str:
        return f"socks5h://127.0.0.1:{self.port}"

    def _build_cmd(self) -> list[str]:
        # BatchMode=yes: fail fast instead of hanging on a password prompt
        #   (key-based auth is required for an unattended tunnel).
        # ExitOnForwardFailure=yes: if the local -D bind fails, ssh exits so we
        #   surface the error rather than a silently-dead proxy.
        # ServerAlive*: detect a dropped connection and let ssh tear down.
        return [
            "ssh",
            "-N",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new",
            "-D", f"127.0.0.1:{self.port}",
            self.target,
        ]

    def start(self) -> None:
        """Spawn ssh and block until the SOCKS5 port accepts connections."""
        cmd = self._build_cmd()
        _log(f"starting: {' '.join(cmd)}")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Ensure the child never outlives us even on an unclean exit.
        atexit.register(self.stop)

        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                err = self._proc.stderr.read() if self._proc.stderr else ""
                raise RuntimeError(
                    f"ssh tunnel to {self.target} exited early "
                    f"(code {self._proc.returncode}): {err.strip() or '<no stderr>'}"
                )
            if _port_open(self.port):
                _log(f"SOCKS5 ready on {self.proxy_url}")
                return
            time.sleep(0.2)

        self.stop()
        raise RuntimeError(
            f"ssh tunnel to {self.target} not ready within {self._connect_timeout:.0f}s"
        )

    def stop(self) -> None:
        """Terminate the ssh child (idempotent)."""
        if self._proc is None:
            return
        proc, self._proc = self._proc, None
        if proc.poll() is None:
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
        _log("stopped")

    def __enter__(self) -> "SshSocksTunnel":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
