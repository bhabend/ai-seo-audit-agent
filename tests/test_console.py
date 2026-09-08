"""The console: a thin layer over three commands, tested without running them.

Nothing here crawls or renders. The runner is checked by mocking subprocess
and building run folders on disk by hand; the app is driven with Streamlit's
own AppTest against a mocked runner, so a page that stops rendering fails
here rather than in front of an operator.
"""

import json
import os
import sys

import pytest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
    __file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import runner  # noqa: E402

APP = os.path.join(APP_DIR, "streamlit_app.py")


# --- the run folder, at several states ---------------------------------------

def make_run_dir(tmp_path, *, files=(), rows=None, host="example.com",
                 name="20260101-000000"):
    """A run folder holding exactly the files a stage would have written."""
    rows = rows or {}
    path = tmp_path / "output" / host / name
    path.mkdir(parents=True)
    for filename in files:
        body = ""
        if filename.endswith(".csv"):
            lines = ["url,status"] + [f"https://x/{i},200"
                                      for i in range(rows.get(filename, 0))]
            body = "\n".join(lines) + "\n"
        elif filename.endswith(".json"):
            body = "{}"
        (path / filename).write_text(body, encoding="utf-8")
    return str(path)


def write_log(tmp_path, lines, name="20260101-000000.log"):
    log_dir = tmp_path / "output" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# --- the log -----------------------------------------------------------------

def test_the_run_folder_is_read_from_the_log(tmp_path):
    log = write_log(tmp_path, [
        "COMMAND=python -m seo_audit.cli --domain https://example.com",
        "Crawling https://example.com ...",
        "RUN_DIR=output/example.com/20260101-000000",
        '{"host": "example.com"}'])

    assert runner.run_dir_of(log) == "output/example.com/20260101-000000"
    assert runner.exit_code_of(log) is None


def test_a_log_without_a_run_folder_line_says_so(tmp_path):
    log = write_log(tmp_path, ["COMMAND=python -m seo_audit.cli", "working"])
    assert runner.run_dir_of(log) is None


def test_the_exit_code_is_read_from_the_end_of_the_log(tmp_path):
    log = write_log(tmp_path, ["working", "Traceback (most recent call last):",
                               "EXIT_CODE=1"])
    assert runner.exit_code_of(log) == 1

    ok = write_log(tmp_path, ["RUN_DIR=x", "EXIT_CODE=0"], name="ok.log")
    assert runner.exit_code_of(ok) == 0


# --- stages, derived from files ----------------------------------------------

def test_a_run_with_no_files_yet_is_starting(tmp_path):
    run_dir = make_run_dir(tmp_path)
    assert runner.stage_of(run_dir) == "starting"
    assert runner.stage_of(None) == "starting"


def test_rows_in_the_raw_crawl_mean_crawling(tmp_path):
    run_dir = make_run_dir(tmp_path, files=("raw_crawl.csv",),
                           rows={"raw_crawl.csv": 12})
    assert runner.stage_of(run_dir) == "crawling"
    assert runner.poll(run_dir)["counters"]["pages"] == 12


def test_a_swept_sitemap_means_the_link_checks(tmp_path):
    run_dir = make_run_dir(
        tmp_path, files=("raw_crawl.csv", "sitemap_sweep.csv"),
        rows={"raw_crawl.csv": 40, "sitemap_sweep.csv": 5})
    assert runner.stage_of(run_dir) == "checking links"


def test_a_pagespeed_file_means_speed_is_being_measured(tmp_path):
    run_dir = make_run_dir(
        tmp_path, files=("raw_crawl.csv", "sitemap_sweep.csv",
                         "pagespeed.csv"),
        rows={"raw_crawl.csv": 40, "sitemap_sweep.csv": 5})
    assert runner.stage_of(run_dir) == "measuring speed"


def test_the_summary_means_scoring_and_findings(tmp_path):
    run_dir = make_run_dir(
        tmp_path, files=("raw_crawl.csv", "sitemap_sweep.csv",
                         "pagespeed.csv", "crawl_summary.json"),
        rows={"raw_crawl.csv": 40})
    assert runner.stage_of(run_dir) == "scoring and findings"


def test_findings_means_done(tmp_path):
    run_dir = make_run_dir(
        tmp_path, files=("raw_crawl.csv", "crawl_summary.json",
                         "findings.json"))
    log = write_log(tmp_path, ["RUN_DIR=x", "EXIT_CODE=0"])
    assert runner.stage_of(run_dir, log) == "done"
    status = runner.poll(run_dir, log)
    assert status["done"] and not status["running"] and not status["failed"]


def test_an_exit_code_without_findings_is_a_failure(tmp_path):
    run_dir = make_run_dir(tmp_path, files=("raw_crawl.csv",),
                           rows={"raw_crawl.csv": 3})
    log = write_log(tmp_path, ["Traceback", "EXIT_CODE=1"])

    assert runner.stage_of(run_dir, log) == "failed"
    status = runner.poll(run_dir, log)
    assert status["failed"] and status["exit_code"] == 1
    assert not status["running"]


def test_a_clean_exit_that_wrote_no_findings_is_still_a_failure(tmp_path):
    """The process said zero and left nothing to report on."""
    run_dir = make_run_dir(tmp_path, files=("raw_crawl.csv",))
    log = write_log(tmp_path, ["EXIT_CODE=0"])
    assert runner.stage_of(run_dir, log) == "failed"


def test_the_counters_count_rows_not_files(tmp_path):
    run_dir = make_run_dir(
        tmp_path, files=("raw_crawl.csv", "page_issues.csv",
                         "crawl_issues.csv", "sitemap_sweep.csv"),
        rows={"raw_crawl.csv": 30, "page_issues.csv": 7,
              "crawl_issues.csv": 4, "sitemap_sweep.csv": 2})
    counters = runner.poll(run_dir)["counters"]

    assert counters["pages"] == 30
    assert counters["issues"] == 11
    assert counters["sitemap urls swept"] == 2


def test_the_log_tail_is_shown_not_the_whole_log(tmp_path):
    log = write_log(tmp_path, [f"line {i}" for i in range(200)])
    tail = runner.poll(None, log)["tail"]
    assert tail.splitlines()[-1] == "line 199"
    assert len(tail.splitlines()) == runner.TAIL_LINES


# --- re-attaching after a restart --------------------------------------------

def test_the_console_reattaches_to_a_run_still_in_flight(tmp_path):
    run_dir = make_run_dir(tmp_path, files=("raw_crawl.csv",),
                           rows={"raw_crawl.csv": 5})
    write_log(tmp_path, [f"RUN_DIR={run_dir}"], name="live.log")

    attached = runner.attach(log_dir=str(tmp_path / "output" / "logs"),
                             root=str(tmp_path / "output"))
    assert attached and attached["run_dir"] == run_dir


def test_a_finished_run_is_not_reattached(tmp_path):
    run_dir = make_run_dir(tmp_path, files=("raw_crawl.csv", "findings.json"))
    write_log(tmp_path, [f"RUN_DIR={run_dir}", "EXIT_CODE=0"], name="old.log")

    assert runner.attach(log_dir=str(tmp_path / "output" / "logs"),
                         root=str(tmp_path / "output")) is None


# --- the commands the runner builds ------------------------------------------

def test_the_audit_command_carries_the_options_it_was_given():
    command = runner.audit_command("https://example.com", {
        "sitemap": "https://example.com/sitemap.xml", "max_pages": 50,
        "workers": 4, "pagespeed": False, "compare": False,
        "external_check_limit": 100})

    assert command[1:4] == ["-m", "seo_audit.cli", "--domain"]
    assert "--no-pagespeed" in command and "--no-compare" in command
    assert command[command.index("--max-pages") + 1] == "50"
    assert command[command.index("--external-check-limit") + 1] == "100"


def test_pagespeed_stays_on_unless_it_is_switched_off():
    assert "--no-pagespeed" not in runner.audit_command("x", {})
    assert "--no-pagespeed" not in runner.audit_command(
        "x", {"pagespeed": True})


def test_start_audit_writes_a_log_and_never_waits(tmp_path, monkeypatch):
    seen = {}

    class FakeProcess:
        pid = 4242

        def wait(self):
            return 0

    def fake_popen(command, stdout=None, stderr=None, cwd=None):
        seen["command"] = command
        seen["stdout"] = stdout
        return FakeProcess()

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    launch = runner.start_audit("https://example.com", {"max_pages": 10},
                                log_dir=str(tmp_path / "logs"))

    assert launch["pid"] == 4242
    assert os.path.exists(launch["log"])
    # The audit runs inside a wrapper that writes the exit code when it ends,
    # so a console that is stopped halfway does not lose how the run finished.
    assert seen["command"][:2] == [sys.executable, "-c"]
    assert seen["command"][3] == launch["log"]
    assert seen["command"][4:7] == [sys.executable, "-m", "seo_audit.cli"]
    body = open(launch["log"], encoding="utf-8").read()
    assert "COMMAND=" in body and "seo_audit.cli" in body


def test_the_wrapper_writes_the_exit_code_even_if_nobody_is_watching(
        tmp_path):
    """The real thing: a command that fails leaves its code in the log."""
    log = str(tmp_path / "wrapped.log")
    open(log, "w").close()
    command = runner.wrapped_command(
        [sys.executable, "-c", "raise SystemExit(3)"], log)

    finished = runner.subprocess.run(command, capture_output=True)
    assert finished.returncode == 3
    assert runner.exit_code_of(log) == 3
    assert runner.stage_of(None, log) == "failed"


def test_the_wrapper_records_a_clean_finish_too(tmp_path):
    log = str(tmp_path / "ok.log")
    open(log, "w").close()
    runner.subprocess.run(runner.wrapped_command(
        [sys.executable, "-c", "print('done')"], log), capture_output=True)
    assert runner.exit_code_of(log) == 0


def test_generate_report_runs_the_report_command(tmp_path, monkeypatch):
    run_dir = make_run_dir(tmp_path, files=("findings.json",))
    open(os.path.join(run_dir, "SEO_Audit_example.docx"), "w").close()
    seen = {}

    class Result:
        returncode = 0
        stdout = "report: fine"
        stderr = ""

    def fake_run(command, capture_output=None, text=None, cwd=None):
        seen["command"] = command
        return Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.generate_report(run_dir, "short")

    assert result["ok"]
    assert result["docx"].endswith("SEO_Audit_example.docx")
    assert seen["command"][1:3] == ["-m", "seo_audit.report"]
    assert seen["command"][seen["command"].index("--format") + 1] == "short"


def test_generate_report_passes_the_long_format_through(tmp_path,
                                                        monkeypatch):
    run_dir = make_run_dir(tmp_path, files=("findings.json",))

    class Result:
        returncode = 1
        stdout = ""
        stderr = "ValidationError: nope"

    monkeypatch.setattr(runner.subprocess, "run",
                        lambda command, **kwargs: Result())
    result = runner.generate_report(run_dir, "long")
    assert not result["ok"] and "ValidationError" in result["output"]


def test_delete_runs_never_passes_delete_all_by_accident(tmp_path,
                                                         monkeypatch):
    seen = []

    class Result:
        returncode = 0
        stdout = "deleted"
        stderr = ""

    def fake_run(command, capture_output=None, text=None, cwd=None):
        seen.append(command)
        return Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    runner.delete_runs("example.com")
    assert "--delete" in seen[0] and "--delete-all" not in seen[0]
    assert "--yes" in seen[0]

    runner.delete_runs("example.com", all=True)
    assert "--delete-all" in seen[1] and "--yes" in seen[1]
    assert seen[1][seen[1].index("--delete-all") + 1] == "example.com"


def test_list_runs_reads_the_folders_on_disk(tmp_path):
    make_run_dir(tmp_path, files=("findings.json", "raw_crawl.csv"),
                 name="20260101-000000")
    make_run_dir(tmp_path, files=("raw_crawl.csv",), name="20260102-000000")
    make_run_dir(tmp_path, files=("findings.json",), host="other.com",
                 name="20260103-000000")

    runs = runner.list_runs(root=str(tmp_path / "output"))
    assert set(runs) == {"example.com", "other.com"}
    assert [r["name"] for r in runs["example.com"]] == ["20260102-000000",
                                                        "20260101-000000"]
    assert runs["example.com"][0]["complete"] is False
    assert runs["example.com"][1]["complete"] is True


# --- the app -----------------------------------------------------------------

@pytest.fixture
def app_runner(monkeypatch, tmp_path):
    """A stand in for the runner, so no page can start a real process."""
    from types import SimpleNamespace

    findings = {
        "meta": {"host": "example.com", "mode": "baseline",
                 "coverage": {"pages_found": 48, "pages_parsed": 40,
                              "sitemap_urls_total": 12}},
        "headline": {"site_score": 72, "mean_page_score": 81.0,
                     "pages_scored": 40, "site_level_deduction": 4,
                     "performance_component": 45.0},
        "prioritised_fixes": [
            {"label": "no links to the page", "severity": "medium",
             "fix_scope": "template", "issue_type": "orphan_page",
             "pages_affected": {"count": 5, "whole": 40,
                                "whole_is": "pages parsed"},
             "targets": None},
            {"label": "links to other websites that are gone",
             "severity": "medium", "fix_scope": "page",
             "issue_type": "external_link_broken",
             "pages_affected": {"count": 8, "whole": 40,
                                "whole_is": "pages parsed"},
             "targets": {"count": 3, "whole": 100,
                         "whole_is": "links to other websites checked"}},
        ],
        "on_page": {"title": {"missing": {"count": 3, "whole": 40,
                                          "whole_is": "pages parsed"}}},
        "comparison": {"previous_run": "20251231-000000",
                       "coverage_changed": True,
                       "notable": [{"metric": "orphan_pages", "previous": 2,
                                    "now": 5, "change": 3,
                                    "note": "not comparable"}]},
    }
    run_dir = str(tmp_path / "output" / "example.com" / "20260101-000000")
    os.makedirs(run_dir)
    with open(os.path.join(run_dir, "findings.json"), "w",
              encoding="utf-8") as handle:
        json.dump(findings, handle)
    open(os.path.join(run_dir, "raw_crawl.csv"), "w").close()

    calls = SimpleNamespace(started=[], reports=[], deletes=[])

    def fake_start(domain, options=None, log_dir=runner.LOG_DIR):
        calls.started.append((domain, options))
        return {"pid": 99, "log": str(tmp_path / "run.log"),
                "command": ["python"], "started": 0.0}

    def fake_poll(run_dir_arg, log_path=None):
        return {"stage": "crawling", "running": True, "done": False,
                "failed": False, "run_dir": run_dir,
                "counters": {"pages": 12, "issues": 3,
                             "sitemap urls swept": 0},
                "exit_code": None, "tail": "working"}

    def fake_report(path, fmt="short", python=None):
        calls.reports.append((path, fmt))
        return {"ok": True, "docx": os.path.join(run_dir, "report.docx"),
                "returncode": 0, "output": "", "command": []}

    def fake_delete(host, all=False, root=runner.OUTPUT_ROOT, python=None):
        calls.deletes.append((host, all))
        return {"ok": True, "returncode": 0, "output": "deleted",
                "command": []}

    open(os.path.join(run_dir, "report.docx"), "wb").close()
    monkeypatch.setattr(runner, "start_audit", fake_start)
    monkeypatch.setattr(runner, "poll", fake_poll)
    monkeypatch.setattr(runner, "attach", lambda **kwargs: None)
    monkeypatch.setattr(runner, "run_dir_of", lambda log: run_dir)
    monkeypatch.setattr(runner, "generate_report", fake_report)
    monkeypatch.setattr(runner, "delete_runs", fake_delete)
    monkeypatch.setattr(runner, "report_usage",
                        lambda path: {"calls": 9, "input_tokens": 100,
                                      "output_tokens": 50})
    monkeypatch.setattr(runner, "list_runs", lambda root=runner.OUTPUT_ROOT: {
        "example.com": [{"host": "example.com", "name": "20260101-000000",
                         "path": run_dir, "when": "2026-01-01 00:00",
                         "size_kb": 12, "files": 2, "complete": True,
                         "report": True}]})
    return calls


def app_test():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_file(APP, default_timeout=30)


def test_the_console_opens_on_the_run_page(app_runner):
    app = app_test().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == "Run audit"
    assert "Run audit" in [header.value for header in app.header]


def test_starting_a_run_launches_one_audit_and_shows_a_stage(app_runner):
    app = app_test().run()
    app.text_input[0].set_value("https://example.com")
    app.button[0].click().run()

    assert not app.exception
    assert app_runner.started == [("https://example.com", app_runner.started
                                   [0][1])]
    domain, options = app_runner.started[0]
    assert options["max_pages"] == 2000 and options["workers"] == 4
    assert any("crawling" in header.value.lower() for header in app.subheader)
    assert any("12" == str(metric.value) for metric in app.metric)


def test_a_run_without_a_domain_is_refused(app_runner):
    app = app_test().run()
    app.button[0].click().run()
    assert app_runner.started == []
    assert app.error


def test_the_results_page_renders_the_fixes_table(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Results").run()

    assert not app.exception
    assert app.metric[0].value == "72"
    frame = app.dataframe[0].value
    labels = [row["Fix"] for row in frame.to_dict("records")]
    assert "no links to the page" in labels
    shares = [row["Pages affected"] for row in frame.to_dict("records")]
    assert "5 of 40 pages parsed" in shares


def test_the_results_page_shows_the_comparison_when_there_is_one(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Results").run()
    headers = [h.value for h in app.subheader]
    assert "Compared with the previous run" in headers


def test_the_report_page_asks_for_the_chosen_format(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Report").run()
    [radio for radio in app.radio if radio.label == "Format"][0].set_value(
        "long").run()
    [button for button in app.button
     if "Generate" in button.label][0].click().run()

    assert not app.exception
    assert app_runner.reports and app_runner.reports[0][1] == "long"


def test_the_report_page_defaults_to_short(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Report").run()
    [button for button in app.button
     if "Generate" in button.label][0].click().run()
    assert app_runner.reports[0][1] == "short"


def test_deleting_needs_the_confirmation_first(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Runs").run()

    delete_all = [button for button in app.button
                  if button.label.startswith("Delete all")][0]
    assert delete_all.disabled, "delete was live before anyone confirmed"
    # AppTest can click a disabled button, which a browser cannot. The
    # handler refuses anyway: the confirmation is a check, not a decoration.
    delete_all.click().run()
    assert app_runner.deletes == []
    assert app.warning

    [box for box in app.checkbox
     if box.label.startswith("Confirm deleting ALL")][0].set_value(True).run()
    [button for button in app.button
     if button.label.startswith("Delete all")][0].click().run()
    assert app_runner.deletes == [("example.com", True)]


def test_deleting_older_runs_keeps_the_newest(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Runs").run()
    [box for box in app.checkbox
     if box.label.startswith("Confirm deleting older")][0].set_value(
         True).run()
    [button for button in app.button
     if button.label.startswith("Delete older")][0].click().run()

    assert app_runner.deletes == [("example.com", False)]


def test_every_page_renders_without_an_exception(app_runner):
    for page in ("Run audit", "Results", "Report", "Runs"):
        app = app_test().run()
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, f"{page} raised {app.exception}"
