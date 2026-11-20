"""The one-command launcher (aeids/start.py): helpers, and stopping child processes."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from aeids.start import Child, Children, local_endpoint, service_endpoints, tablelens_python


def test_local_endpoint() -> None:
    assert local_endpoint("http://127.0.0.1:8000", 80) == ("127.0.0.1", 8000)
    assert local_endpoint("redis://localhost/0", 6379) == ("localhost", 6379)
    assert local_endpoint("https://tablelens.example.com", 443) is None


def test_service_endpoints() -> None:
    eps = service_endpoints("postgresql+psycopg://u:p@127.0.0.1:5433/aeids",
                            "redis://127.0.0.1:6379/0", "http://127.0.0.1:6333")
    assert eps == {"postgres": ("127.0.0.1", 5433), "redis": ("127.0.0.1", 6379),
                   "qdrant": ("127.0.0.1", 6333)}
    # SQLite and remote services need nothing from docker compose
    assert service_endpoints("sqlite:///x.db", "redis://cache.internal:6379/0",
                             "http://qdrant.internal:6333") == {}


def _python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_tablelens_python_order(tmp_path: Path) -> None:
    root = tmp_path / "AEIDS"
    assert tablelens_python(root, env={}) is None
    old = _python(tmp_path / "IntelligencePDF" / ".venv-tablelens" / "bin" / "python")
    assert tablelens_python(root, env={}) == old
    own = _python(root / "tablelens" / ".venv" / "bin" / "python")
    assert tablelens_python(root, env={}) == own
    chosen = _python(tmp_path / "custom" / "python")
    assert tablelens_python(root, env={"AEIDS_TABLELENS_PYTHON": str(chosen)}) == chosen


def _child(tmp_path: Path, name: str, code: str, **kw) -> Child:
    return Child(name, [sys.executable, "-c", code], tmp_path, **kw)


SLEEPER = "import time\nwhile True: time.sleep(0.1)"
# ignores the first SIGINT (like RQ finishing a job), stops on the second
STUBBORN = (
    "import signal, time\n"
    "n = 0\n"
    "def h(*_):\n"
    "    global n; n += 1\n"
    "    if n == 2: raise SystemExit(0)\n"
    "signal.signal(signal.SIGINT, h)\n"
    "print('ready', flush=True)\n"
    "while True: time.sleep(0.05)\n"
)
DEAF = (
    "import signal, time\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "while True: time.sleep(0.05)\n"
)


def _wait_output(capsys: pytest.CaptureFixture[str], text: str, count: int = 1,
                 seconds: float = 10) -> str:
    deadline = time.monotonic() + seconds
    seen = ""
    while time.monotonic() < deadline:
        seen += capsys.readouterr().out
        if seen.count(text) >= count:
            return seen
        time.sleep(0.05)
    raise AssertionError(f"{text!r} not printed {count}x; got {seen!r}")


def test_stop_interrupts_every_child(tmp_path: Path) -> None:
    kids = Children()
    kids.start(_child(tmp_path, "a", SLEEPER, stop_timeout=5))
    kids.start(_child(tmp_path, "b", SLEEPER, stop_timeout=5))
    assert kids.exited() is None
    kids.stop()
    assert all(c.proc.poll() is not None for c in kids.children)


def test_second_interrupt_and_kill(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    kids = Children()
    kids.start(_child(tmp_path, "worker", STUBBORN, stop_timeout=0.5, second_interrupt=True))
    kids.start(_child(tmp_path, "deaf", DEAF, stop_timeout=0.5))
    _wait_output(capsys, "| ready", count=2)  # both have set their signal handlers
    kids.stop()
    worker, deaf = kids.children
    assert worker.proc.returncode == 0  # stopped by the second SIGINT
    assert deaf.proc.returncode == -9  # ignored SIGINT, killed
    assert "deaf did not stop in time" in capsys.readouterr().out


def test_output_is_prefixed_and_exit_is_seen(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    kids = Children()
    kids.start(_child(tmp_path, "api", "print('hello')", stop_timeout=1))
    deadline = time.monotonic() + 10
    while kids.exited() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert kids.exited().name == "api"
    _wait_output(capsys, "api      | hello")


def test_offers_to_stop_other_projects_containers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ports held by another project's containers: on "y" they are stopped and this project's
    own services are started with docker compose."""
    import aeids.start as start

    eps = {"postgres": ("127.0.0.1", 5432), "redis": ("127.0.0.1", 6379)}
    state = {"up": True}
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str | None:
        calls.append(args)
        if args[:2] == ("compose", "ps"):
            return ""  # none of this project's services run
        if args[0] == "ps":
            return {"publish=5432": "old-postgres-1\n", "publish=6379": "old-redis-1\n"}[args[2]]
        if args[0] == "stop":
            state["up"] = False
            return "\n".join(args[1:])
        raise AssertionError(args)

    class Compose:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        assert cmd == ["docker", "compose", "up", "-d"]
        state["up"] = True
        return Compose()

    monkeypatch.setattr(start, "_docker", fake_docker)
    monkeypatch.setattr(start, "port_open", lambda h, p, timeout=1.0: state["up"])
    monkeypatch.setattr(start.subprocess, "run", fake_run)

    monkeypatch.setattr(start, "ask", lambda q: False)  # "no": keep using them
    assert start.ensure_services(eps)
    assert not [c for c in calls if c[0] == "stop"]

    monkeypatch.setattr(start, "ask", lambda q: "old-postgres-1, old-redis-1" in q)  # "yes"
    assert start.ensure_services(eps)
    assert ("stop", "old-postgres-1", "old-redis-1") in calls
    assert state["up"]  # docker compose up -d ran


def test_ask_without_terminal_is_no(monkeypatch: pytest.MonkeyPatch) -> None:
    import aeids.start as start

    monkeypatch.setattr(start.sys.stdin, "isatty", lambda: False, raising=False)
    assert start.ask("? ") is False
