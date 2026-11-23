"""Windowless launcher for the scheduled daily run.

Task Scheduler starts this with ``venv\\Scripts\\pythonw.exe -m src.daily_runner``.
pythonw has no console, so there is no black window to close by accident
(closing it used to kill the run with exit 0xC000013A).

The task fires at 7:00 AM on weekdays and again, as a catch-up, at logon and
on unlock (a sleeping laptop misses the 7:00 trigger). This module makes sure
the full pipeline runs at most once per weekday:

- weekends and the small hours (before 06:30) are skipped;
- a day counts as done once today's report exists or a run finished OK;
- a run that started under an hour ago is treated as still running;
- a failed day is retried at most MAX_ATTEMPTS times, and not at all after
  an FMP quota error (retrying can't help before the 00:00 UTC reset).

The pipeline itself runs as a child ``python.exe -u -m src.main`` with
CREATE_NO_WINDOW, its output appended to reports/run_log.txt between
"Run started" / "Run finished" lines. Use ``--force`` to bypass the guard and
``--check`` to print the decision without running anything.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT_DIR / "reports"
LOG_FILE = REPORTS_DIR / "run_log.txt"
STATE_FILE = REPORTS_DIR / "daily_run_state.json"

EARLIEST = time(6, 30)
MAX_ATTEMPTS = 2
RUN_TIMEOUT = timedelta(minutes=50)  # the scheduled task itself stops at 1h
STALE_AFTER = timedelta(hours=1)
EXIT_QUOTA = 2  # mirrors src.main.EXIT_QUOTA (not imported: keep this module light)


def load_state(path: Path = STATE_FILE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def report_exists(day: str, reports_dir: Path = REPORTS_DIR) -> bool:
    return (reports_dir / f"{day}-report.md").exists()


def decide(now: datetime, state: dict, report_done: bool, force: bool = False) -> tuple[bool, str]:
    """Return (should_run, reason). Pure: no I/O, easy to test."""
    if force:
        return True, "forced"
    if now.weekday() >= 5:
        return False, "weekend"
    if now.time() < EARLIEST:
        return False, f"before {EARLIEST.strftime('%H:%M')}"
    if report_done:
        return False, "already ran today (report exists)"

    today = now.date().isoformat()
    if state.get("date") != today:
        return True, "first attempt today"

    status = state.get("status")
    if status == "ok":
        return False, "already ran today"
    if status == "running":
        try:
            started = datetime.fromisoformat(state.get("started", ""))
        except ValueError:
            started = None
        if started and now - started < STALE_AFTER:
            return False, "a run is already in progress"
    if status == "quota":
        return False, "FMP quota exhausted today; waiting for the reset"
    attempts = int(state.get("attempts", 0))
    if attempts >= MAX_ATTEMPTS:
        return False, f"gave up after {attempts} failed attempts today"
    return True, f"retry (attempt {attempts + 1} of {MAX_ATTEMPTS})"


def _child_python() -> str:
    """python.exe next to the running interpreter (pythonw.exe has no stdout)."""
    exe = Path(sys.executable)
    candidate = exe.with_name("python.exe")
    return str(candidate if candidate.exists() else exe)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%a %m/%d/%Y %H:%M:%S")


def run_pipeline(reason: str) -> int:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with LOG_FILE.open("a", encoding="utf-8", errors="replace") as log:
        log.write("================================\n")
        log.write(f"Run started {_stamp(datetime.now())} (no window; {reason})\n")
        log.flush()
        proc = subprocess.Popen(
            [_child_python(), "-u", "-m", "src.main"],
            cwd=ROOT_DIR, stdout=log, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags,
        )
        try:
            code = proc.wait(timeout=RUN_TIMEOUT.total_seconds())
        except subprocess.TimeoutExpired:
            # Kill the whole tree: the venv python.exe is a launcher with a child.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=flags)
            proc.wait()
            code = 124
            log.write(f"\nRun timed out after {int(RUN_TIMEOUT.total_seconds() // 60)} minutes.\n")
        log.write(f"Run finished {_stamp(datetime.now())} (exit {code})\n")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Once-per-weekday windowless daily run")
    parser.add_argument("--force", action="store_true", help="run even if today's run already happened")
    parser.add_argument("--check", action="store_true", help="print the decision and exit")
    args = parser.parse_args(argv)

    now = datetime.now()
    today = now.date().isoformat()
    state = load_state()
    should, reason = decide(now, state, report_exists(today), force=args.force)

    if args.check:
        if sys.stdout:
            print(f"{'RUN' if should else 'SKIP'}: {reason}")
        return 0
    if not should:
        state["last_check"] = {"at": now.isoformat(timespec="seconds"), "skipped": reason}
        save_state(state)
        return 0

    attempts = int(state.get("attempts", 0)) if state.get("date") == today else 0
    state = {"date": today, "status": "running", "started": now.isoformat(timespec="seconds"),
             "attempts": attempts + 1, "reason": reason}
    save_state(state)

    code = run_pipeline(reason)

    state.update(finished=datetime.now().isoformat(timespec="seconds"), exit=code,
                 status="ok" if code == 0 else "quota" if code == EXIT_QUOTA else "failed")
    save_state(state)
    return code


if __name__ == "__main__":
    sys.exit(main())
