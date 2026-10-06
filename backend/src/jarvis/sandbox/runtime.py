"""Sandbox runtimes. The sandbox is a disposable Docker container — never the host.

Container hardening (DockerRuntime.start): unprivileged user, all capabilities dropped, no-new-privileges, read-only
root filesystem (writable /workspace volume + size-capped tmpfs), CPU/memory/pid limits, and an `--internal` network
whose only exit is the `jarvis-egress` allowlist proxy (sandbox/egress_proxy.py). No host paths are mounted.
Everything here shells out to the `docker` CLI with argument lists (no shell interpolation of model text).

Each user gets their own container, volume, internal network and egress proxy (`names_for`), so one person's files,
browser state and network window are never visible to another. The first owner keeps the original V4 names so an
existing workspace survives the upgrade.
"""
from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

IMAGE = "jarvis-sandbox:1"
OWNER = "usr_owner"
LABEL = "jarvis.sandbox=1"


@dataclass(frozen=True)
class Names:
    container: str
    proxy: str
    network: str
    volume: str


def names_for(uid: str) -> Names:
    if uid == OWNER:
        return Names("jarvis-sbx", "jarvis-egress", "jarvis-sbx-net", "jarvis-sbx-work")
    suf = hashlib.sha256(uid.encode()).hexdigest()[:10]
    return Names(f"jarvis-sbx-{suf}", f"jarvis-egress-{suf}", f"jarvis-sbx-net-{suf}", f"jarvis-sbx-work-{suf}")

BUILD_DIR = Path(__file__).resolve().parents[3] / "sandbox"
MAX_OUTPUT = 20000
SCREEN = (1280, 800)


class SandboxUnavailable(Exception):
    pass


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False


class Runtime(Protocol):
    name: str
    simulated: bool

    def available(self) -> tuple[bool, str]: ...
    def status(self) -> dict: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def reset(self) -> None: ...
    def exec(self, command: str, timeout_s: int, stdin: bytes | None = None) -> ExecResult: ...
    def read_bytes(self, path: str, max_bytes: int) -> bytes | None: ...   # raw file bytes (no output clipping)
    def set_egress(self, domains: list[str], until_unix: float) -> None: ...
    def screenshot(self) -> bytes: ...
    def input(self, args: list[str]) -> ExecResult: ...       # xdotool argument list


def _clip(b: bytes) -> str:
    s = b.decode("utf-8", "replace")
    return s if len(s) <= MAX_OUTPUT else s[:MAX_OUTPUT] + f"\n…[{len(s) - MAX_OUTPUT} more characters truncated]"


class DockerRuntime:
    name = "docker"
    simulated = False

    def __init__(self, uid: str = OWNER) -> None:
        self.uid = uid
        self.n = names_for(uid)
        self._avail: tuple[float, tuple[bool, str]] | None = None

    def _docker(self, *args: str, timeout: float = 60, stdin: bytes | None = None, check: bool = True):
        try:
            p = subprocess.run(["docker", *args], input=stdin, capture_output=True, timeout=timeout)
        except FileNotFoundError:
            raise SandboxUnavailable("Docker isn't installed or not on PATH for this WSL distro.") from None
        if check and p.returncode != 0:
            raise SandboxUnavailable(f"docker {args[0]} failed: {p.stderr.decode('utf-8', 'replace')[:300]}")
        return p

    def available(self) -> tuple[bool, str]:
        if self._avail and time.monotonic() - self._avail[0] < 60:
            return self._avail[1]
        if not shutil.which("docker"):
            res = (False, "Docker isn't available in WSL. Enable Docker Desktop → Settings → Resources → WSL integration "
                          "for this distro, then restart JARVIS.")
        else:
            try:
                p = self._docker("info", "--format", "{{.ServerVersion}}", timeout=15, check=False)
                res = (True, f"Docker {p.stdout.decode().strip()}") if p.returncode == 0 else \
                    (False, "Docker is installed but the daemon isn't reachable (is Docker Desktop running?).")
            except (SandboxUnavailable, subprocess.TimeoutExpired):
                res = (False, "Docker didn't respond.")
        self._avail = (time.monotonic(), res)
        return res

    def image_present(self) -> bool:
        return self._docker("image", "inspect", IMAGE, check=False, timeout=20).returncode == 0

    def build(self) -> str:
        p = self._docker("build", "-t", IMAGE, str(BUILD_DIR), timeout=1800)
        return _clip(p.stdout[-4000:])

    def count_running(self) -> int:
        """Sandboxes running across all users (for the org's concurrency cap)."""
        p = self._docker("ps", "-q", "--filter", f"label={LABEL}", check=False, timeout=15)
        return len([x for x in p.stdout.split() if x]) if p.returncode == 0 else 0

    def _running(self, name: str) -> bool:
        p = self._docker("inspect", "-f", "{{.State.Running}}", name, check=False, timeout=15)
        return p.returncode == 0 and p.stdout.strip() == b"true"

    def status(self) -> dict:
        ok, why = self.available()
        if not ok:
            return {"runtime": "docker", "available": False, "detail": why}
        return {"runtime": "docker", "available": True, "detail": why, "image": self.image_present(),
                "running": self._running(self.n.container), "proxy": self._running(self.n.proxy)}

    def start(self) -> None:
        ok, why = self.available()
        if not ok:
            raise SandboxUnavailable(why)
        if not self.image_present():
            raise SandboxUnavailable("The sandbox image isn't built yet (Settings → Sandbox → Build, ~5 minutes once).")
        if self._docker("network", "inspect", self.n.network, check=False).returncode != 0:
            self._docker("network", "create", "--internal", self.n.network)
        if not self._running(self.n.proxy):
            self._docker("rm", "-f", self.n.proxy, check=False)
            self._docker("run", "-d", "--name", self.n.proxy, "--label", LABEL + "-proxy", "--network", "bridge",
                         "--cap-drop", "ALL",
                         "--security-opt", "no-new-privileges", "--memory", "128m", "--pids-limit", "64", "--read-only",
                         "--tmpfs", "/run/allow:size=1m,uid=1000", "--user", "1000:1000", IMAGE,
                         "python", "/opt/jarvis/egress_proxy.py")
            self._docker("network", "connect", "--alias", "egress", self.n.network, self.n.proxy)
            self.set_egress([], 0)
        if not self._running(self.n.container):
            self._docker("rm", "-f", self.n.container, check=False)
            self._docker("run", "-d", "--name", self.n.container, "--label", LABEL, "--network", self.n.network,
                         "--cpus", "2", "--memory", "2g",
                         "--pids-limit", "512", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                         "--tmpfs", "/tmp:size=1g,uid=1000", "--mount", f"type=volume,src={self.n.volume},dst=/workspace",  # nosec B108 tmpfs inside the sandbox container, not the host
                         "-e", "HTTP_PROXY=http://egress:3128", "-e", "HTTPS_PROXY=http://egress:3128",
                         "-e", "http_proxy=http://egress:3128", "-e", "https_proxy=http://egress:3128",
                         "--user", "1000:1000", IMAGE)

    def stop(self) -> None:
        self._docker("rm", "-f", self.n.container, check=False)

    def reset(self) -> None:
        self._docker("rm", "-f", self.n.container, check=False)
        self._docker("volume", "rm", "-f", self.n.volume, check=False)

    def exec(self, command: str, timeout_s: int, stdin: bytes | None = None) -> ExecResult:
        t = time.perf_counter()
        args = ["exec", "-i", "-w", "/workspace", self.n.container, "timeout", "-k", "5", str(timeout_s), "bash", "-lc", command]
        try:
            p = self._docker(*args, timeout=timeout_s + 20, stdin=stdin, check=False)
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", "timed out", int((time.perf_counter() - t) * 1000), True)
        return ExecResult(p.returncode, _clip(p.stdout), _clip(p.stderr), int((time.perf_counter() - t) * 1000),
                          p.returncode == 124)

    def read_bytes(self, path: str, max_bytes: int) -> bytes | None:
        # Binary-safe and unclipped (exec output is clipped for the model); the caller validated `path`.
        p = self._docker("exec", self.n.container, "head", "-c", str(max_bytes + 1), "--", path, timeout=60, check=False)
        if p.returncode != 0:
            return None
        if len(p.stdout) > max_bytes:
            raise SandboxUnavailable(f"File is larger than {max_bytes} bytes.")
        return p.stdout

    def set_egress(self, domains: list[str], until_unix: float) -> None:
        body = json.dumps({"domains": sorted({d.lower() for d in domains}), "until": until_unix}).encode()
        self._docker("exec", "-i", self.n.proxy, "sh", "-c", "cat > /run/allow/allow.json", stdin=body, timeout=15)

    def screenshot(self) -> bytes:
        p = self._docker("exec", self.n.container, "import", "-window", "root", "png:-", timeout=30)
        return p.stdout

    def input(self, args: list[str]) -> ExecResult:
        t = time.perf_counter()
        p = self._docker("exec", self.n.container, "xdotool", *args, timeout=30, check=False)
        return ExecResult(p.returncode, _clip(p.stdout), _clip(p.stderr), int((time.perf_counter() - t) * 1000))


class FakeRuntime:
    """Contract fake (simulated=True): records calls and returns scripted results. Never runs anything on the host."""
    name = "fake"
    simulated = True

    def __init__(self) -> None:
        self.running = False
        self.calls: list[tuple] = []
        self.files: dict[str, bytes] = {}
        self.egress: tuple[list[str], float] = ([], 0)
        self.outputs: dict[str, ExecResult] = {}
        self.inputs: list[list[str]] = []
        self.shot = b"\x89PNG\r\n\x1a\n" + b"0" * 64

    def available(self) -> tuple[bool, str]:
        return True, "simulated runtime"

    def count_running(self) -> int:
        return 1 if self.running else 0

    def status(self) -> dict:
        return {"runtime": "fake", "available": True, "detail": "simulated", "image": True, "running": self.running,
                "proxy": self.running}

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def reset(self) -> None:
        self.running, self.files = False, {}

    def exec(self, command: str, timeout_s: int, stdin: bytes | None = None) -> ExecResult:
        self.calls.append(("exec", command, timeout_s))
        if command.startswith("mkdir -p") and "cat >" in command and stdin is not None:
            self.files[shlex.split(command)[-1]] = stdin
            return ExecResult(0, "", "", 1)
        if command.startswith("cat -- "):
            path = shlex.split(command)[-1]
            if path in self.files:
                return ExecResult(0, self.files[path].decode(), "", 1)
            return ExecResult(1, "", "No such file", 1)
        for k, v in self.outputs.items():
            if k in command:
                return v
        return ExecResult(0, f"ran: {command}", "", 3)

    def read_bytes(self, path: str, max_bytes: int) -> bytes | None:
        data = self.files.get(path)
        if data is not None and len(data) > max_bytes:
            raise SandboxUnavailable(f"File is larger than {max_bytes} bytes.")
        return data

    def set_egress(self, domains: list[str], until_unix: float) -> None:
        self.egress = (sorted(domains), until_unix)

    def screenshot(self) -> bytes:
        return self.shot

    def input(self, args: list[str]) -> ExecResult:
        self.inputs.append(args)
        return ExecResult(0, "", "", 1)


_runtimes: dict[str, Runtime] = {}
_override: Runtime | None = None


def get_runtime(uid: str = OWNER) -> Runtime:
    """The user's sandbox. A test override (set_runtime) applies to every user unless one was set for that user."""
    if uid in _runtimes:
        return _runtimes[uid]
    if _override is not None:
        return _override
    _runtimes[uid] = DockerRuntime(uid)
    return _runtimes[uid]


def set_runtime(r: Runtime | None, uid: str | None = None) -> None:
    global _override
    if uid is None:
        _override = r
        _runtimes.clear()
    elif r is None:
        _runtimes.pop(uid, None)
    else:
        _runtimes[uid] = r
