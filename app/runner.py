"""What the console is allowed to do: start commands and read files.

No audit logic lives here. Every function either launches one of the three
commands the package already ships, or reads something off disk that those
commands wrote. If a question cannot be answered from a file in the run
folder, this module does not answer it.

The audit runs as its own process with its output redirected to
`output/logs/<timestamp>.log`, so closing the browser, restarting Streamlit
or losing the session does not stop it. Progress is read back from the files
the run is writing, never from anything held in memory, and a run is finished
when `findings.json` exists. The exit code is appended to the log when the
process ends, which is how a run that died is told apart from one still
working.
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


def start_audit(domain: str, options: Optional[Dict[str, Any]] = None,
                log_dir: str = LOG_DIR) -> Dict[str, Any]:
    """Launch the audit as its own process. Returns its pid and log path."""
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir,
                            f"{time.strftime('%Y%m%d-%H%M%S')}.log")
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
    return {"pid": process.pid, "log": log_path, "command": command,
            "started": time.time()}


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
    findings.json. Newest first, because that is the one an operator means.
    """
    if not os.path.isdir(log_dir):
        return None
    logs = sorted((os.path.join(log_dir, name)
                   for name in os.listdir(log_dir) if name.endswith(".log")),
                  key=os.path.getmtime, reverse=True)
    for log_path in logs:
        if exit_code_of(log_path) is not None:
            continue
        run_dir = run_dir_of(log_path) or newest_run_dir(
            since=os.path.getmtime(log_path), root=root)
        if run_dir and _exists(run_dir, "findings.json"):
            continue
        return {"log": log_path, "run_dir": run_dir}
    return None


# --- the other two commands -------------------------------------------------

def generate_report(run_dir: str, fmt: str = "short",
                    python: Optional[str] = None) -> Dict[str, Any]:
    """Run the report command over a finished run folder."""
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
            })
        if found:
            runs[host] = found
    return runs


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
