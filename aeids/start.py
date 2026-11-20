"""Start everything for local work with one command (``./start.sh``).

    ./start.sh                      # services, migrations, TableLens, API, worker
    ./start.sh --no-tablelens       # without parsing: search and questions on parsed reports
    ./start.sh --api-port 8002

In order: Postgres, Qdrant and Redis are checked and started with ``docker compose up -d``
only if one is not reachable; the database is migrated (``alembic upgrade head``); then
TableLens (``tablelens/app.py``, its own Python env), the AEIDS API and the worker run as
child processes, their output in this window with a name in front of each line (and in
``logs/<name>.log``, which the dashboard's Logs page reads). Ctrl+C stops
the three (the Docker services keep running). If one of them exits on its own, the others are
stopped too, so nothing half-runs unnoticed.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import re
import time
from collections.abc import Sequence
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
TABLELENS_DIR = ROOT / "tablelens"
TABLELENS_PORT = 8000  # tablelens/app.py serves 127.0.0.1:8000 (fixed in its __main__)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
COLORS = {"tablelens": "35", "api": "36", "worker": "33", "start": "32"}


# --- helpers (pure, tested) -------------------------------------------------

def local_endpoint(url: str, default_port: int) -> tuple[str, int] | None:
    """(host, port) of a URL on this machine, or None for a remote URL."""
    parts = urlsplit(url)
    if parts.hostname not in LOCAL_HOSTS:
        return None
    return parts.hostname, parts.port or default_port


def service_endpoints(database_url: str, redis_url: str, qdrant_url: str) -> dict[str, tuple[str, int]]:
    """The local Docker Compose services AEIDS needs, by name."""
    out: dict[str, tuple[str, int]] = {}
    db = make_url(database_url)
    if db.get_backend_name() == "postgresql" and db.host in LOCAL_HOSTS:
        out["postgres"] = (db.host, db.port or 5432)
    for name, url, port in (("redis", redis_url, 6379), ("qdrant", qdrant_url, 6333)):
        ep = local_endpoint(url, port)
        if ep:
            out[name] = ep
    return out


def tablelens_python(root: Path = ROOT, env: dict[str, str] | None = None) -> Path | None:
    """The Python of the TableLens env: $AEIDS_TABLELENS_PYTHON, tablelens/.venv, or the old
    ../IntelligencePDF/.venv-tablelens. None when there is none."""
    env = dict(os.environ) if env is None else env
    candidates = [Path(env["AEIDS_TABLELENS_PYTHON"])] if env.get("AEIDS_TABLELENS_PYTHON") else []
    candidates += [root / "tablelens" / ".venv" / "bin" / "python",
                   root.parent / "IntelligencePDF" / ".venv-tablelens" / "bin" / "python"]
    return next((p for p in candidates if p.is_file()), None)


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def healthy(url: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(url.rstrip("/") + "/health", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


# --- output -----------------------------------------------------------------

_print_lock = threading.Lock()
_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class LogFiles:
    """Appends each program's lines to ``<dir>/<name>.log`` with a timestamp; a file over
    ``max_bytes`` is moved to ``<name>.log.1`` (one old file kept)."""

    def __init__(self, directory: Path | None, max_bytes: int = 5 * 1024 * 1024) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, line: str) -> None:
        if self.directory is None:
            return
        path = self.directory / f"{name}.log"
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        with self._lock:
            try:
                if path.exists() and path.stat().st_size > self.max_bytes:
                    path.replace(path.with_name(path.name + ".1"))
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{stamp} {_ANSI.sub('', line)}\n")
            except OSError:
                pass  # logging to a file must never stop the programs


_logs = LogFiles(None)


def say(name: str, line: str) -> None:
    label = f"{name:<9}|"
    if _color:
        label = f"\033[{COLORS.get(name, '0')}m{label}\033[0m"
    with _print_lock:
        print(f"{label} {line}", flush=True)
    _logs.write(name, line)


def _pump(name: str, stream: IO[str]) -> None:
    for line in stream:
        say(name, line.rstrip("\n"))


# --- child processes --------------------------------------------------------

@dataclass
class Child:
    name: str
    cmd: Sequence[str]
    cwd: Path
    stop_timeout: float  # seconds after the first SIGINT before it is forced
    second_interrupt: bool = False  # RQ: a second SIGINT means "stop now" (cold shutdown)
    proc: subprocess.Popen[str] | None = field(default=None, repr=False)


class Children:
    """Child processes in their own process groups, so Ctrl+C reaches only this launcher,
    which then stops them in order."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.children: list[Child] = []
        self.env = {**(os.environ if env is None else env), "PYTHONUNBUFFERED": "1"}

    def start(self, child: Child) -> None:
        child.proc = subprocess.Popen(
            list(child.cmd), cwd=child.cwd, env=self.env, text=True, bufsize=1,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        threading.Thread(target=_pump, args=(child.name, child.proc.stdout), daemon=True).start()
        self.children.append(child)

    def exited(self) -> Child | None:
        """The first child that has exited, if any."""
        return next((c for c in self.children if c.proc and c.proc.poll() is not None), None)

    def stop(self) -> None:
        """SIGINT each child (last started first), wait, then force what is left."""
        for child in reversed(self.children):
            proc = child.proc
            if proc is None or proc.poll() is not None:
                continue
            say("start", f"stopping {child.name} ...")
            _signal(proc, signal.SIGINT)
            if _waited(proc, child.stop_timeout):
                continue
            if child.second_interrupt:
                _signal(proc, signal.SIGINT)
                if _waited(proc, 5):
                    continue
            say("start", f"{child.name} did not stop in time; killing it")
            _signal(proc, signal.SIGKILL)
            proc.wait()


def _waited(proc: subprocess.Popen[str], seconds: float) -> bool:
    try:
        proc.wait(seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _signal(proc: subprocess.Popen[str], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)  # the whole group: llama-server under TableLens too
    except ProcessLookupError:
        pass


# --- steps ------------------------------------------------------------------

def _tail(result: subprocess.CompletedProcess[str]) -> None:
    for line in (result.stderr or result.stdout or "").strip().splitlines()[-8:]:
        say("start", line)


def _docker(*args: str) -> str | None:
    """stdout of a docker command, or None when docker is missing / fails."""
    try:
        result = subprocess.run(["docker", *args], cwd=ROOT, text=True, capture_output=True,
                                timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def foreign_services(endpoints: dict[str, tuple[str, int]]) -> dict[str, str]:
    """Services whose port is answered by something other than this repo's compose project
    (e.g. the old ``intelligencepdf`` containers), with the container name when known."""
    ours = _docker("compose", "ps", "--status", "running", "--services")
    if ours is None:
        return {}
    out: dict[str, str] = {}
    for name, (_host, port) in endpoints.items():
        if name not in ours.split():
            other = (_docker("ps", "--filter", f"publish={port}", "--format", "{{.Names}}") or "").split()
            out[name] = other[0] if other else "another program"
    return out


def ask(question: str) -> bool:
    """A yes/no question in the terminal; False when nobody can answer (no terminal)."""
    if not sys.stdin.isatty():
        return False
    try:
        with _print_lock:
            answer = input(question)
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def ensure_services(endpoints: dict[str, tuple[str, int]], wait_seconds: float = 90,
                    offer_stop: bool = True) -> bool:
    down = [n for n, (h, p) in endpoints.items() if not port_open(h, p)]
    if not down:
        say("start", f"services up: {', '.join(endpoints) or 'none needed'}")
        foreign = foreign_services(endpoints)
        for name, other in foreign.items():
            say("start", f"note: {name} (port {endpoints[name][1]}) is {other}, not this project's "
                         f"docker compose service")
        containers = sorted({c for c in foreign.values() if c != "another program"})
        if offer_stop and containers and ask(
                f"Stop {', '.join(containers)} so this project's own services start? "
                f"Their data is kept (docker start <name> brings them back). [y/N] "):
            if _docker("stop", *containers) is None:
                say("start", "error: docker stop failed")
                return False
            say("start", f"stopped {', '.join(containers)}")
            return ensure_services(endpoints, wait_seconds, offer_stop=False)
        return True
    say("start", f"not running: {', '.join(down)} -> docker compose up -d")
    try:
        result = subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, text=True,
                                capture_output=True)
    except FileNotFoundError:
        say("start", "error: docker not found. Install Docker / colima, or start the services yourself.")
        return False
    if result.returncode != 0:
        _tail(result)
        say("start", "error: docker compose failed. Is Docker running (colima start)? "
                     "Is another project using the same ports?")
        return False
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if all(port_open(h, p) for h, p in endpoints.values()):
            say("start", "services up")
            return True
        time.sleep(1)
    say("start", "error: services did not come up in time (see docker compose ps / logs)")
    return False


def wait_for_database(database_url: str, wait_seconds: float = 60) -> bool:
    from aeids.db.session import make_engine

    engine = make_engine(database_url)
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                with engine.connect() as conn:
                    conn.execute(text("select 1"))
                return True
            except SQLAlchemyError as exc:
                if _refused_login(exc):  # retrying cannot help
                    say("start", "error: the database refused the login (user, password or database "
                                 "name in AEIDS_DATABASE_URL). If the note above names another "
                                 "project's container, stop it (docker stop <name>) and run "
                                 "./start.sh again: this project's own Postgres then starts.")
                    return False
                if time.monotonic() >= deadline:
                    say("start", f"error: database not reachable ({type(exc).__name__}); check .env")
                    return False
                time.sleep(1)
    finally:
        engine.dispose()


def _refused_login(exc: BaseException) -> bool:
    message = str(getattr(exc, "orig", None) or exc)
    return any(s in message for s in ("password authentication failed", "does not exist",
                                      "no password supplied"))


def migrate() -> bool:
    say("start", "database: alembic upgrade head")
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT,
                            text=True, capture_output=True)
    if result.returncode != 0:
        _tail(result)
        say("start", "error: migration failed")
        return False
    return True


def wait_healthy(name: str, url: str, children: Children, stop: threading.Event,
                 wait_seconds: float) -> bool:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline and not stop.is_set():
        if healthy(url):
            return True
        if children.exited():
            return False
        time.sleep(1)
    if not stop.is_set():
        say("start", f"warning: {name} not answering at {url} after {wait_seconds:.0f} s")
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="./start.sh", description="Start AEIDS and TableLens.")
    ap.add_argument("--no-tablelens", action="store_true",
                    help="do not start TableLens (uploads wait until it runs)")
    ap.add_argument("--no-worker", action="store_true", help="do not start the background worker")
    ap.add_argument("--api-port", type=int, default=8001)
    args = ap.parse_args(argv)

    if not (ROOT / ".env").is_file():
        say("start", "error: no .env. Run: cp .env.example .env  (then set AEIDS_DB_PASSWORD)")
        return 1
    from aeids.config import get_settings

    global _logs
    settings = get_settings()
    log_dir = settings.log_dir if settings.log_dir.is_absolute() else ROOT / settings.log_dir
    _logs = LogFiles(log_dir)

    if not ensure_services(service_endpoints(settings.database_url, settings.redis_url,
                                             settings.qdrant_url)):
        return 1
    if not wait_for_database(settings.database_url) or not migrate():
        return 1
    if port_open("127.0.0.1", args.api_port):
        say("start", f"error: port {args.api_port} is already in use (AEIDS already running?). "
                     "Stop it, or use --api-port.")
        return 1

    children = Children()
    if not args.no_tablelens:
        local = local_endpoint(settings.tablelens_url, 80)
        if healthy(settings.tablelens_url):
            say("start", f"TableLens already running at {settings.tablelens_url}")
        elif local is None or local[1] != TABLELENS_PORT:
            say("start", f"TableLens is set to {settings.tablelens_url}, not this machine's "
                         f":{TABLELENS_PORT}; not starting it here")
        elif (py := tablelens_python()) is None:
            say("start", "warning: TableLens env not found, so TableLens is not started; uploads "
                         "wait until it runs. Create it: cd tablelens && uv venv --python 3.11 .venv "
                         "&& uv pip install --python .venv/bin/python -r requirements.txt")
        elif port_open("127.0.0.1", TABLELENS_PORT):
            say("start", f"warning: port {TABLELENS_PORT} is used by something that is not TableLens; "
                         "TableLens is not started")
        else:
            # on SIGINT TableLens saves what a running job has read (its lifespan allows 25 s)
            children.start(Child("tablelens", [str(py), "app.py"], TABLELENS_DIR, stop_timeout=35))

    children.start(Child("api", [sys.executable, "-m", "uvicorn", "aeids.api.app:create_app",
                                 "--factory", "--host", "127.0.0.1", "--port", str(args.api_port)],
                         ROOT, stop_timeout=10))
    if not args.no_worker:
        # RQ finishes the running job on the first SIGINT; a long parse is cut by the second
        # one and picked up again by `python -m aeids.worker requeue`
        children.start(Child("worker", [sys.executable, "-m", "aeids.worker"], ROOT,
                             stop_timeout=10, second_interrupt=True))

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    def announce() -> None:
        api = f"http://127.0.0.1:{args.api_port}"
        if wait_healthy("API", api, children, stop, 60):
            say("start", f"AEIDS API ready: {api}/docs")
        if any(c.name == "tablelens" for c in children.children):
            if wait_healthy("TableLens", settings.tablelens_url, children, stop, 300):
                say("start", f"TableLens ready: {settings.tablelens_url}")
        if not stop.is_set():
            say("start", "everything is running. Ctrl+C stops it")

    threading.Thread(target=announce, daemon=True).start()

    code = 0
    while not stop.wait(0.5):
        if (gone := children.exited()) is not None:
            say("start", f"{gone.name} exited (code {gone.proc.returncode}); stopping the rest")
            code = 1
            break
    for sig in (signal.SIGINT, signal.SIGTERM):  # a second Ctrl+C must not cut the clean stop short
        signal.signal(sig, signal.SIG_IGN)
    children.stop()
    say("start", "stopped. Docker services keep running (docker compose stop to stop them)")
    return code


if __name__ == "__main__":
    sys.exit(main())
