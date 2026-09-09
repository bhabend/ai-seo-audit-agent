"""What the console is allowed to do: start commands and read files.

No audit logic lives here. Every function either launches one of the three
commands the package already ships, or reads something off disk that those
commands wrote. If a question cannot be answered from a file in the run
folder, this module does not answer it.

The audit runs as its own process with its output redirected to
`output/logs/audit-<timestamp>.log`, so closing the browser, restarting
Streamlit or losing the session does not stop it. Progress is read back from
the files the run is writing, never from anything held in memory, and a run
is finished when `findings.json` exists. The exit code is appended to the log
when the process ends, which is how a run that died is told apart from one
still working.

A file counts as a run log only when it carries the COMMAND and STARTED
header this module writes. Anything else that lands in that folder, someone's
redirected stdout included, is ignored rather than counted as a run nobody
started.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

OUTPUT_ROOT = "output"
LOG_DIR = os.path.join(OUTPUT_ROOT, "logs")
RUN_DIR_PREFIX = "RUN_DIR="
EXIT_PREFIX = "EXIT_CODE="
TAIL_LINES = 25

# Every file a finished run folder carries, in the order a client would read
# them. The console offers these for download and counts them for progress.
RUN_FILES = ("raw_crawl.csv", "crawl_summary.json", "sitemap_sweep.csv",
             "crawl_issues.csv", "audit_pages.csv", "page_issues.csv",
             "pagespeed.csv", "findings.json")

# The stages a run passes through, each proved by a file the run wrote.
# Scoring and findings share one window on purpose: the summary is written
# between them, so no file separates them, and the console says both rather
# than inventing a boundary.
STAGES = ("starting", "crawling", "checking links", "measuring speed",
          "scoring and findings", "done", "failed")


# --- starting a run ---------------------------------------------------------

def audit_command(domain: str, options: Optional[Dict[str, Any]] = None,
                  python: Optional[str] = None) -> List[str]:
    """The exact command line the console would run. Nothing hidden."""
    options = options or {}
    command = [python or sys.executable, "-m", "seo_audit.cli",
               "--domain", str(domain)]
    if options.get("sitemap"):
        command += ["--sitemap", str(options["sitemap"])]
    for key, flag in (("max_pages", "--max-pages"),
                      ("workers", "--workers"),
                      ("max_depth", "--max-depth"),
                      ("sweep_limit", "--sweep-limit"),
                      ("external_check_limit", "--external-check-limit")):
        if options.get(key) is not None:
            command += [flag, str(options[key])]
    if options.get("pagespeed") is False:
        command.append("--no-pagespeed")
    if options.get("compare") is False:
        command.append("--no-compare")
    if options.get("compare_to"):
        command += ["--compare-to", str(options["compare_to"])]
    return command


# The audit is launched inside a one line wrapper whose only job is to run
# it and then write the exit code at the end of the log. The wrapper owns
# that, not this console: if Streamlit is stopped halfway through a run, the
# run still finishes and still records how it ended, which is the difference
# between "failed" and "we stopped watching".
WRAPPER = (
    "import subprocess, sys;"
    "code = subprocess.call(sys.argv[2:]);"
    "open(sys.argv[1], 'a', encoding='utf-8').write("
    "chr(10) + 'EXIT_CODE=' + str(code) + chr(10));"
    "sys.exit(code)"
)


def wrapped_command(command: List[str], log_path: str,
                    python: Optional[str] = None) -> List[str]:
    return [python or sys.executable, "-c", WRAPPER, log_path] + command


# A run whose log has not been touched for this long, and which never wrote
# an exit code, was killed with the machine rather than finished. Nothing can
# be assumed about it, but it must not block the console forever.
STALE_AFTER_HOURS = 24

# The two lines start_audit stamps at the top of every log it opens. They are
# what makes a file a run log: not its name, and not the folder it sits in.
COMMAND_PREFIX = "COMMAND="
STARTED_PREFIX = "STARTED="
HEADER_LINES = 8
# New logs are named distinctively as well, so a folder listing reads
# clearly. Old logs are bare timestamps and are still recognised, because
# the header decides and the name only narrows the search.
LOG_PREFIX = "audit-"


def is_run_log(path: str) -> bool:
    """True when this file is a log the runner itself opened.

    The console was once started with its own output redirected into
    output/logs/. That file ended in .log and had no exit code, so it read as
    an audit in flight and the Start button stayed disabled with nothing
    running. Anything can be written into a folder; only a run log carries
    the header a run wrote, so that is what is checked. The name is a cheap
    first pass, nothing more.
    """
    if not path or not path.lower().endswith(".log"):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            head = [next(handle, "") for _ in range(HEADER_LINES)]
    except OSError:
        return False
    return (any(line.startswith(COMMAND_PREFIX) for line in head)
            and any(line.startswith(STARTED_PREFIX) for line in head))


def _unfinished(log_dir: str) -> List[str]:
    """Run logs with no exit code yet, newest first."""
    if not os.path.isdir(log_dir):
        return []
    logs = [os.path.join(log_dir, name) for name in os.listdir(log_dir)]
    return sorted((path for path in logs
                   if is_run_log(path) and exit_code_of(path) is None),
                  key=os.path.getmtime, reverse=True)


def is_stale(log_path: str, hours: float = STALE_AFTER_HOURS) -> bool:
    """True when a run log has no exit code and has gone quiet for too long."""
    if not log_path or not os.path.exists(log_path):
        return False
    if not is_run_log(log_path):
        return False
    if exit_code_of(log_path) is not None:
        return False
    return (time.time() - os.path.getmtime(log_path)) > hours * 3600


def stale_runs(log_dir: str = LOG_DIR) -> List[str]:
    """Runs that stopped without saying so. Shown, never silently ignored."""
    return [path for path in _unfinished(log_dir) if is_stale(path)]


def running_runs(log_dir: str = LOG_DIR) -> List[str]:
    """Logs with no exit code line: the runs still going, newest first.

    The wrapper writes EXIT_CODE at the end of every run it launches, so a
    log without one is a run that has not finished. Nothing here asks the
    operating system about processes and nothing is remembered in memory.

    A log that has been silent for a day is left out: the machine it was
    running on is long gone, and one lost run must not stop every run after
    it. The Runs page still lists it, so it disappears from nobody's view.
    """
    return [path for path in _unfinished(log_dir) if not is_stale(path)]


def start_audit(domain: str, options: Optional[Dict[str, Any]] = None,
                log_dir: str = LOG_DIR) -> Dict[str, Any]:
    """Launch the audit as its own process. Returns its pid and log path.

    One at a time. Two crawls of the same site at once are two sets of
    counters, two folders and one confused operator, so a second start is
    refused while the first has not written its exit code.
    """
    busy = running_runs(log_dir)
    if busy:
        return {"ok": False, "running": busy, "log": None, "pid": None,
                "error": f"A run is already going: {busy[0]}. Wait for it to "
                         f"finish, or look at its log."}
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir, f"{LOG_PREFIX}{time.strftime('%Y%m%d-%H%M%S')}.log")
    command = audit_command(domain, options)

    with open(log_path, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(f"COMMAND={' '.join(command)}\n")
        handle.write(f"STARTED={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")

    handle = open(log_path, "a", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            wrapped_command(command, log_path), stdout=handle,
            stderr=subprocess.STDOUT, cwd=os.getcwd())
    finally:
        # The child holds its own handle on the file from here on.
        handle.close()
    return {"ok": True, "pid": process.pid, "log": log_path,
            "command": command, "started": time.time()}


# --- reading a run back -----------------------------------------------------

def read_log(log_path: str) -> str:
    if not log_path or not os.path.exists(log_path):
        return ""
    with open(log_path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def run_dir_of(log_path: str) -> Optional[str]:
    """The folder the run wrote to, from the line the command prints."""
    for line in read_log(log_path).splitlines():
        if line.startswith(RUN_DIR_PREFIX):
            return line[len(RUN_DIR_PREFIX):].strip() or None
    return None


def exit_code_of(log_path: str) -> Optional[int]:
    for line in reversed(read_log(log_path).splitlines()):
        if line.startswith(EXIT_PREFIX):
            try:
                return int(line[len(EXIT_PREFIX):].strip())
            except ValueError:
                return None
    return None


def newest_run_dir(since: float = 0.0, root: str = OUTPUT_ROOT
                   ) -> Optional[str]:
    """The newest run folder created since a moment, for a run in flight.

    The folder cannot be named until the homepage answers and the real host
    is known, so a run announces it only at the end. Until then the console
    finds it the way a person would: the newest folder under output/.
    """
    newest, newest_time = None, since
    if not os.path.isdir(root):
        return None
    for host in os.listdir(root):
        host_dir = os.path.join(root, host)
        if not os.path.isdir(host_dir) or host_dir == LOG_DIR:
            continue
        for name in os.listdir(host_dir):
            path = os.path.join(host_dir, name)
            if not os.path.isdir(path):
                continue
            when = os.path.getmtime(path)
            if when >= newest_time:
                newest, newest_time = path, when
    return newest


def row_count(path: str) -> int:
    """Data rows in a streamed CSV, header not counted."""
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        return max(0, sum(1 for _line in handle) - 1)


def _exists(run_dir: str, name: str) -> bool:
    return bool(run_dir) and os.path.exists(os.path.join(run_dir, name))


def stage_of(run_dir: Optional[str], log_path: Optional[str] = None) -> str:
    """Which stage a run is in, from the files it has written so far."""
    code = exit_code_of(log_path) if log_path else None
    if run_dir and _exists(run_dir, "findings.json"):
        return "done"
    if code is not None:
        # The process ended without findings.json, whatever it said on the
        # way out. That is a failed run, not a finished one.
        return "failed"
    if not run_dir:
        return "starting"
    if _exists(run_dir, "crawl_summary.json"):
        return "scoring and findings"
    if _exists(run_dir, "pagespeed.csv"):
        return "measuring speed"
    if row_count(os.path.join(run_dir, "sitemap_sweep.csv")):
        return "checking links"
    if _exists(run_dir, "raw_crawl.csv"):
        return "crawling"
    return "starting"


def poll(run_dir: Optional[str], log_path: Optional[str] = None
         ) -> Dict[str, Any]:
    """Everything the console shows while a run is in flight."""
    log = read_log(log_path) if log_path else ""
    stage = stage_of(run_dir, log_path)
    counters = {"pages": 0, "issues": 0, "sitemap urls swept": 0}
    if run_dir:
        counters = {
            "pages": row_count(os.path.join(run_dir, "raw_crawl.csv")),
            "issues": (row_count(os.path.join(run_dir, "page_issues.csv"))
                       + row_count(os.path.join(run_dir, "crawl_issues.csv"))),
            "sitemap urls swept": row_count(
                os.path.join(run_dir, "sitemap_sweep.csv")),
        }
    return {
        "stage": stage,
        "running": stage not in ("done", "failed"),
        "done": stage == "done",
        "failed": stage == "failed",
        "run_dir": run_dir,
        "counters": counters,
        "exit_code": exit_code_of(log_path) if log_path else None,
        "tail": "\n".join(log.splitlines()[-TAIL_LINES:]),
    }


def attach(log_dir: str = LOG_DIR, root: str = OUTPUT_ROOT
           ) -> Optional[Dict[str, Any]]:
    """The run still in flight, if this console was restarted under one.

    A run is in flight when its log has no exit code and its folder has no
    findings.json. Only one can be, because only one is ever started, so
    there is nothing to choose between.
    """
    for log_path in running_runs(log_dir):
        run_dir = run_dir_of(log_path) or newest_run_dir(
            since=os.path.getmtime(log_path), root=root)
        if run_dir and _exists(run_dir, "findings.json"):
            continue
        return {"log": log_path, "run_dir": run_dir}
    return None


def bound_run_dir(log_path: str, root: str = OUTPUT_ROOT) -> Optional[str]:
    """The folder belonging to one log, and to no other run.

    The audit announces its folder in the log once the host is settled. Until
    then the only candidate is a folder written after that log began, so a
    run started later can never be mistaken for this one.
    """
    if not log_path or not os.path.exists(log_path):
        return None
    named = run_dir_of(log_path)
    if named:
        return named
    started = os.path.getmtime(log_path)
    newer = [path for path in running_runs(os.path.dirname(log_path))
             if path != log_path and os.path.getmtime(path) > started]
    if newer:
        # Another run began after this one. Guessing by folder age could hand
        # back its folder, so this one says it does not know yet.
        return None
    return newest_run_dir(since=started, root=root)


# --- the other two commands -------------------------------------------------

def has_openai_key() -> bool:
    """Whether a key is configured. The value is never read out of here."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv ships with the package
        pass
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


NO_KEY_MESSAGE = "No OpenAI key configured; add OPENAI_API_KEY to .env"


def generate_report(run_dir: str, fmt: str = "short",
                    python: Optional[str] = None) -> Dict[str, Any]:
    """Run the report command over a finished run folder.

    The long format needs a key. Finding that out from a traceback after the
    click is a poor way to learn it, so it is checked first.
    """
    if fmt == "long" and not has_openai_key():
        return {"ok": False, "docx": None, "returncode": None,
                "message": NO_KEY_MESSAGE, "output": NO_KEY_MESSAGE,
                "command": []}
    command = [python or sys.executable, "-m", "seo_audit.report",
               "--run", run_dir, "--format", fmt]
    result = subprocess.run(command, capture_output=True, text=True,
                            cwd=os.getcwd())
    docx = None
    for name in sorted(os.listdir(run_dir)) if os.path.isdir(run_dir) else []:
        if name.lower().endswith(".docx"):
            docx = os.path.join(run_dir, name)
    return {"ok": result.returncode == 0, "docx": docx,
            "returncode": result.returncode,
            "output": (result.stderr or "") + (result.stdout or ""),
            "command": command}


def report_usage(run_dir: str) -> Optional[Dict[str, Any]]:
    """What the long format cost, when it has been run. Short writes none."""
    import json

    path = os.path.join(run_dir, "report_usage.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, OSError):
        return None


def attach_gsc(run_dir: str, upload: Any, filename: str = "export.zip",
               python: Optional[str] = None) -> Dict[str, Any]:
    """Join an uploaded Search Console export to a run.

    The upload is written to a temporary file, handed to the command, and
    deleted. Nothing of the client's export is kept: what survives is
    gsc_join.csv, which is the audit's own answer, not their data.
    """
    import tempfile

    suffix = os.path.splitext(filename or "")[1].lower() or ".zip"
    data = upload.read() if hasattr(upload, "read") else upload
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    try:
        handle.write(data if isinstance(data, bytes) else str(data).encode())
        handle.close()
        command = [python or sys.executable, "-m", "seo_audit.gsc",
                   "--run", run_dir, "--export", handle.name]
        result = subprocess.run(command, capture_output=True, text=True,
                                cwd=os.getcwd())
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    return {"ok": result.returncode == 0, "returncode": result.returncode,
            "output": (result.stderr or "") + (result.stdout or ""),
            "command": command}


def run_mode(run_dir: str) -> str:
    """What this run can answer: baseline, or full once an export is on it."""
    import json

    path = os.path.join(run_dir, "findings.json")
    if not os.path.exists(path):
        return "unknown"
    try:
        with open(path, encoding="utf-8") as handle:
            findings = json.load(handle)
    except (ValueError, OSError):
        return "unknown"
    return (findings.get("meta") or {}).get("mode", "baseline")


def list_runs(root: str = OUTPUT_ROOT) -> Dict[str, List[Dict[str, Any]]]:
    """Every run folder on disk, newest first, per host."""
    runs: Dict[str, List[Dict[str, Any]]] = {}
    if not os.path.isdir(root):
        return runs
    for host in sorted(os.listdir(root)):
        host_dir = os.path.join(root, host)
        if not os.path.isdir(host_dir) or os.path.abspath(host_dir) == \
                os.path.abspath(LOG_DIR):
            continue
        found = []
        for name in sorted(os.listdir(host_dir), reverse=True):
            path = os.path.join(host_dir, name)
            if not os.path.isdir(path):
                continue
            files = sorted(os.listdir(path))
            size = sum(os.path.getsize(os.path.join(path, f)) for f in files
                       if os.path.isfile(os.path.join(path, f)))
            found.append({
                "host": host, "name": name, "path": path,
                "when": time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(os.path.getmtime(path))),
                "size_kb": round(size / 1024),
                "files": len(files),
                "complete": "findings.json" in files,
                "report": any(f.lower().endswith(".docx") for f in files),
                "search": "gsc_join.csv" in files,
            })
        if found:
            runs[host] = found
    return runs


def delete_host(host: str, root: str = OUTPUT_ROOT,
                python: Optional[str] = None) -> Dict[str, Any]:
    """Remove a host and every run it has, through the clean command.

    The only call that leaves a host with nothing, so it is its own function
    rather than another flag on delete_runs: nobody reaches it by passing a
    boolean by mistake.
    """
    command = [python or sys.executable, "-m", "seo_audit.clean",
               "--delete-host", host, "--root", root, "--yes"]
    result = subprocess.run(command, capture_output=True, text=True,
                            cwd=os.getcwd())
    return {"ok": result.returncode == 0, "returncode": result.returncode,
            "output": (result.stdout or "") + (result.stderr or ""),
            "command": command}


def delete_runs(host: str, all: bool = False, root: str = OUTPUT_ROOT,
                python: Optional[str] = None) -> Dict[str, Any]:
    """Delete a host's runs through the clean command, never by hand.

    `all` is the only way --delete-all is ever passed, because that is the
    one flag that removes a host's newest run.
    """
    flag = "--delete-all" if all else "--delete"
    command = [python or sys.executable, "-m", "seo_audit.clean",
               flag, host, "--root", root, "--yes"]
    result = subprocess.run(command, capture_output=True, text=True,
                            cwd=os.getcwd())
    return {"ok": result.returncode == 0, "returncode": result.returncode,
            "output": (result.stdout or "") + (result.stderr or ""),
            "command": command}
