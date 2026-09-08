"""The console: a thin layer over three commands, tested without running them.

Nothing here crawls or renders. The runner is checked by mocking subprocess
and building run folders on disk by hand; the app is driven with Streamlit's
own AppTest against a mocked runner, so a page that stops rendering fails
here rather than in front of an operator.
"""

import json
import os
import sys
import time

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

    calls = SimpleNamespace(started=[], reports=[], deletes=[], hosts=[],
                            running=[], exit_code=None, polled=[],
                            run_dir=run_dir)

    log_path = str(tmp_path / "run.log")
    open(log_path, "w", encoding="utf-8").write("working\n")

    def fake_start(domain, options=None, log_dir=runner.LOG_DIR):
        calls.started.append((domain, options))
        return {"ok": True, "pid": 99, "log": log_path,
                "command": ["python"], "started": 0.0}

    def fake_poll(run_dir_arg, log_path=None):
        calls.polled.append((run_dir_arg, log_path))
        return {"stage": "crawling", "running": calls.exit_code is None,
                "done": False,
                "failed": False, "run_dir": run_dir,
                "counters": {"pages": 12, "issues": 3,
                             "sitemap urls swept": 0},
                "exit_code": calls.exit_code, "tail": "working"}

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
    def fake_delete_host(host, root=runner.OUTPUT_ROOT, python=None):
        calls.hosts.append(host)
        return {"ok": True, "returncode": 0, "output": "gone", "command": []}

    monkeypatch.setattr(runner, "delete_host", fake_delete_host)
    monkeypatch.setattr(runner, "running_runs",
                        lambda log_dir=runner.LOG_DIR: calls.running)
    monkeypatch.setattr(runner, "exit_code_of", lambda path: calls.exit_code)
    monkeypatch.setattr(runner, "bound_run_dir",
                        lambda path, root=runner.OUTPUT_ROOT: run_dir)
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
    assert len(app_runner.started) == 1
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


# --- session 11 item 1: the progress block refreshes on a timer -------------

def test_the_progress_block_is_a_fragment_that_reruns_every_four_seconds():
    """Registered with run_every, not redrawn only when someone clicks.

    Read off the decorated function itself: Streamlit keeps the interval in
    the wrapper's closure. Nothing is patched, so this cannot leave a half
    built fragment behind for the next app to trip over.
    """
    import streamlit_app

    assert streamlit_app.REFRESH_SECONDS == 4
    intervals = [cell.cell_contents
                 for cell in (streamlit_app._progress.__closure__ or ())
                 if isinstance(cell.cell_contents, (int, float))
                 and not isinstance(cell.cell_contents, bool)]
    assert intervals == [4], f"the fragment was registered with {intervals}"


def test_the_progress_block_reads_the_log_it_is_bound_to(app_runner,
                                                         tmp_path):
    app = app_test().run()
    app.text_input[0].set_value("https://example.com")
    app.button[0].click().run()

    assert not app.exception
    bound = app_runner.started and str(tmp_path / "run.log")
    assert app_runner.polled, "the progress block never polled"
    assert all(log == bound for _run_dir, log in app_runner.polled)
    assert any("crawling" in header.value.lower()
               for header in app.subheader)


def test_a_finished_run_still_renders_its_last_state(app_runner):
    app_runner.exit_code = 0
    app = app_test().run()
    app.text_input[0].set_value("https://example.com")
    app.button[0].click().run()
    assert not app.exception
    assert app_runner.polled


# --- item 2: the Results page shows the report's own tables -----------------

def test_the_results_tables_are_the_ones_the_report_builds(app_runner,
                                                           tmp_path):
    """The console and the client see the same rows, from one builder."""
    from seo_audit import report

    app = app_test().run()
    app.sidebar.radio[0].set_value("Results").run()
    assert not app.exception

    findings = json.load(open(os.path.join(
        app_runner.run_dir, "findings.json"), encoding="utf-8"))
    coverage = (findings.get("meta") or {}).get("coverage") or {}
    expected = report.short_section_rows("on_page", findings["on_page"],
                                         coverage)

    frames = [frame.value.values.tolist() for frame in app.dataframe]
    assert expected in frames, f"the on page table is not rendered: {expected}"
    header = report.SHORT_SECTION_COLUMNS
    assert header == ["Finding", "Count", "Severity", "Action"]


def test_the_section_builder_gives_the_document_what_it_always_gave():
    """The extraction moved code, not output: the In place row still closes."""
    from seo_audit import report

    section = {"title": {"missing": {"count": 3, "whole": 40,
                                     "whole_is": "pages parsed"}}}
    rows = report.short_section_rows("on_page", section,
                                     {"pages_parsed": 40, "pages_found": 48})
    assert rows[0][:2] == ["Page title missing", "3 of 40 pages parsed"]
    assert rows[-1][0] == report.IN_PLACE
    parts, in_place_row = report.short_section_parts(
        "on_page", section, {"pages_parsed": 40, "pages_found": 48})
    assert rows == parts + [in_place_row]


# --- item 3: the cost is stated before the click ----------------------------

def test_the_long_format_says_what_it_costs_before_it_is_run(app_runner):
    import streamlit_app

    app = app_test().run()
    app.sidebar.radio[0].set_value("Report").run()
    captions = [caption.value for caption in app.caption]
    assert streamlit_app.COST_NOTE in captions

    [radio for radio in app.radio if radio.label == "Format"][0].set_value(
        "long").run()
    assert streamlit_app.COST_NOTE in [c.value for c in app.caption]
    assert "about one cent" in streamlit_app.COST_NOTE
    assert app_runner.reports == [], "nothing was generated to show a price"


# --- item 4: one run at a time, and the view bound to it --------------------

def test_a_second_start_is_refused_while_one_is_running(tmp_path,
                                                        monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "20260101-000000.log").write_text("working\n",
                                                 encoding="utf-8")

    called = []
    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: called.append(a))
    launch = runner.start_audit("https://example.com", {},
                                log_dir=str(log_dir))

    assert launch["ok"] is False
    assert "already going" in launch["error"]
    assert called == [], "a second audit was launched anyway"


def test_a_finished_log_does_not_block_the_next_run(tmp_path, monkeypatch):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "old.log").write_text("done\nEXIT_CODE=0\n", encoding="utf-8")

    class FakeProcess:
        pid = 7

        def wait(self):
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen",
                        lambda *a, **k: FakeProcess())
    launch = runner.start_audit("https://example.com", {},
                                log_dir=str(log_dir))
    assert launch["ok"] and launch["pid"] == 7


def test_running_runs_lists_only_the_unfinished_logs(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "a.log").write_text("EXIT_CODE=0\n", encoding="utf-8")
    (log_dir / "b.log").write_text("still going\n", encoding="utf-8")

    running = runner.running_runs(str(log_dir))
    assert [os.path.basename(p) for p in running] == ["b.log"]


def test_the_bound_view_ignores_a_folder_from_a_later_run(tmp_path):
    """A run started after this one must never steal the counters."""
    log_dir = tmp_path / "output" / "logs"
    log_dir.mkdir(parents=True)
    mine = log_dir / "20260101-000000.log"
    mine.write_text("working\n", encoding="utf-8")

    older = make_run_dir(tmp_path, files=("raw_crawl.csv",),
                         name="20260101-000001")
    os.utime(mine, (os.path.getmtime(older) - 5,) * 2)

    # A second run appears, newer than mine, with its own folder.
    later = log_dir / "20260102-000000.log"
    later.write_text("working\n", encoding="utf-8")
    newer_dir = make_run_dir(tmp_path, files=("raw_crawl.csv",),
                             host="other.com", name="20260102-000000")

    bound = runner.bound_run_dir(str(mine), root=str(tmp_path / "output"))
    assert bound != newer_dir, "the view followed someone else's run"

    # Once the log names its folder, there is nothing left to infer.
    mine.write_text(f"working\nRUN_DIR={older}\n", encoding="utf-8")
    assert runner.bound_run_dir(str(mine),
                                root=str(tmp_path / "output")) == older


def test_the_start_button_is_disabled_while_a_run_is_going(app_runner):
    app_runner.running = ["output/logs/20260101-000000.log"]
    app_runner.exit_code = None
    app = app_test().run()

    assert app.warning, "nothing said a run was already going"
    assert app.button[0].disabled, "start was live during a run"


# --- item 5: removing a host ------------------------------------------------

def test_removing_a_host_needs_the_name_typed(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Runs").run()

    remove = [button for button in app.button
              if button.label.startswith("Remove")][0]
    assert remove.disabled, "remove was live before the name was typed"
    remove.click().run()
    assert app_runner.hosts == []

    boxes = [box for box in app.text_input
             if box.label.startswith("Type example.com")]
    assert boxes, "no box to type the host name into"
    boxes[0].set_value("example.co").run()
    assert [b for b in app.button
            if b.label.startswith("Remove")][0].disabled

    [box for box in app.text_input
     if box.label.startswith("Type example.com")][0].set_value(
         "example.com").run()
    [button for button in app.button
     if button.label.startswith("Remove")][0].click().run()
    assert app_runner.hosts == ["example.com"]


def test_delete_host_calls_the_clean_command_with_yes(tmp_path, monkeypatch):
    seen = []

    class Result:
        returncode = 0
        stdout = "gone"
        stderr = ""

    monkeypatch.setattr(runner.subprocess, "run",
                        lambda command, **kwargs: (seen.append(command),
                                                   Result())[1])
    result = runner.delete_host("example.com", root=str(tmp_path))

    assert result["ok"]
    assert "--delete-host" in seen[0] and "--yes" in seen[0]
    assert seen[0][seen[0].index("--delete-host") + 1] == "example.com"


def test_clean_removes_every_run_of_one_host_and_no_other(tmp_path, capsys):
    from seo_audit.clean import main as clean_main

    root = tmp_path / "output"
    for host, names in (("gone.example", ("20260101-000000",
                                          "20260102-000000")),
                        ("kept.example", ("20260101-000000",))):
        for name in names:
            path = root / host / name
            path.mkdir(parents=True)
            (path / "findings.json").write_text("{}", encoding="utf-8")

    code = clean_main(["--delete-host", "gone.example", "--root", str(root),
                       "--yes"])
    capsys.readouterr()

    assert code == 0
    assert not (root / "gone.example").exists(), "the host folder is still here"
    assert (root / "kept.example" / "20260101-000000").exists()


def test_clean_refuses_to_remove_a_host_without_yes(tmp_path, capsys):
    from seo_audit.clean import main as clean_main

    root = tmp_path / "output"
    path = root / "gone.example" / "20260101-000000"
    path.mkdir(parents=True)
    (path / "findings.json").write_text("{}", encoding="utf-8")

    code = clean_main(["--delete-host", "gone.example", "--root", str(root)])
    output = capsys.readouterr()

    assert code == 1
    assert path.exists(), "runs went without --yes"
    assert "--yes" in output.err


# --- session 12: stale runs, the key check, and attaching an export --------

def test_a_run_that_stopped_a_day_ago_no_longer_blocks_the_console(tmp_path):
    """One reboot must not lock the console out for good."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    stale = log_dir / "old.log"
    stale.write_text("crawling\n", encoding="utf-8")
    old = time.time() - 25 * 3600
    os.utime(stale, (old, old))

    assert runner.is_stale(str(stale))
    assert runner.running_runs(str(log_dir)) == []
    assert runner.stale_runs(str(log_dir)) == [str(stale)]


def test_a_run_that_started_an_hour_ago_still_blocks(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    live = log_dir / "live.log"
    live.write_text("crawling\n", encoding="utf-8")
    recent = time.time() - 3600
    os.utime(live, (recent, recent))

    assert not runner.is_stale(str(live))
    assert runner.running_runs(str(log_dir)) == [str(live)]
    assert runner.stale_runs(str(log_dir)) == []


def test_a_finished_log_is_never_stale(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    done = log_dir / "done.log"
    done.write_text("EXIT_CODE=0\n", encoding="utf-8")
    old = time.time() - 100 * 3600
    os.utime(done, (old, old))
    assert not runner.is_stale(str(done))
    assert runner.stale_runs(str(log_dir)) == []


def test_the_long_report_says_when_there_is_no_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "has_openai_key", lambda: False)
    called = []
    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: called.append(a))

    result = runner.generate_report(str(tmp_path), "long")
    assert result["ok"] is False
    assert result["message"] == runner.NO_KEY_MESSAGE
    assert called == [], "the report command ran without a key"


def test_the_short_report_needs_no_key(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "has_openai_key", lambda: False)

    class Result:
        returncode = 0
        stdout = "written"
        stderr = ""

    monkeypatch.setattr(runner.subprocess, "run",
                        lambda *a, **k: Result())
    assert runner.generate_report(str(tmp_path), "short")["ok"]


def test_attaching_an_export_deletes_the_upload_it_was_given(tmp_path,
                                                             monkeypatch):
    """The client's data is joined and dropped; only our answer is kept."""
    seen = {}

    class Result:
        returncode = 0
        stdout = "matched 10 of 10"
        stderr = ""

    def fake_run(command, capture_output=None, text=None, cwd=None):
        seen["command"] = list(command)
        seen["existed"] = os.path.exists(command[command.index("--export")
                                                 + 1])
        return Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.attach_gsc(str(tmp_path), b"Top pages,Clicks\n",
                               "export.csv")

    assert result["ok"]
    assert seen["command"][1:3] == ["-m", "seo_audit.gsc"]
    assert seen["existed"], "the command was handed nothing to read"
    temp_path = seen["command"][seen["command"].index("--export") + 1]
    assert temp_path.endswith(".csv")
    assert not os.path.exists(temp_path), "the upload was left on disk"


def test_the_mode_of_a_run_is_read_from_its_findings(tmp_path):
    run_dir = make_run_dir(tmp_path, files=("findings.json",))
    with open(os.path.join(run_dir, "findings.json"), "w",
              encoding="utf-8") as handle:
        json.dump({"meta": {"mode": "full"}}, handle)
    assert runner.run_mode(run_dir) == "full"

    plain = make_run_dir(tmp_path, files=("findings.json",),
                         name="20260102-000000")
    assert runner.run_mode(plain) == "baseline"
    assert runner.run_mode(str(tmp_path / "nowhere")) == "unknown"


def test_the_results_page_offers_the_uploader_and_shows_the_section(
        app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Results").run()

    assert not app.exception
    assert any("Search performance" == header.value
               for header in app.subheader)
    labels = [element.label for element in app.get("file_uploader")]
    assert any("Search Console" in label for label in labels)


def test_the_report_page_says_which_mode_the_run_is_in(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Report").run()
    captions = " ".join(caption.value for caption in app.caption)
    assert "mode" in captions


def test_the_results_page_renders_the_search_tables_once_attached(
        app_runner, tmp_path):
    """After an attach, the console shows the same tables the report does."""
    from seo_audit import report

    findings_path = os.path.join(app_runner.run_dir, "findings.json")
    with open(findings_path, encoding="utf-8") as handle:
        findings = json.load(handle)
    findings["search_performance"] = {
        "provided": True, "date_range": "2026-01-01 to 2026-06-30",
        "coverage_note": "Read Pages.csv (pages).",
        "totals": {"impressions": 5000, "clicks": 120,
                   "pages_in_export": 20, "queries_in_export": 40},
        "join": {"matched": {"count": 18, "whole": 20,
                             "whole_is": "pages in the export"},
                 "not_crawled": {"count": 2, "whole": 20,
                                 "whole_is": "pages in the export"},
                 "crawled_not_in_export": {"count": 4, "whole": 22,
                                           "whole_is": "pages crawled"}},
        "near_miss_queries": [{"query": "office space pune", "clicks": 3,
                               "impressions": 900, "ctr_percent": 0.3,
                               "position": 7.2}],
        "cannibalised_queries": [], "cannibalisation_available": False,
        "issue_counts": {"gsc_orphan_page_with_clicks": 2},
        "wholes": {"pages_with_impressions": 18, "sitemap_pages": 20,
                   "queries": 40},
    }
    with open(findings_path, "w", encoding="utf-8") as handle:
        json.dump(findings, handle)

    app = app_test().run()
    app.sidebar.radio[0].set_value("Results").run()
    assert not app.exception

    frames = [frame.value.values.tolist() for frame in app.dataframe]
    coverage = report.search_coverage_rows(findings["search_performance"])
    assert coverage in frames, f"the join coverage table is missing: {coverage}"
    issue_rows = report.search_issue_rows(findings["search_performance"])
    assert issue_rows in frames, "the search findings table is missing"
    assert any(metric.value == "5000" for metric in app.metric)


# --- session 14: an Excel export reaches the command as a workbook ---------

def test_an_excel_upload_keeps_its_extension_for_the_reader(tmp_path,
                                                            monkeypatch):
    """The reader tells the container from the suffix, so it must survive."""
    seen = {}

    class Result:
        returncode = 0
        stdout = "matched"
        stderr = ""

    def fake_run(command, capture_output=None, text=None, cwd=None):
        export = command[command.index("--export") + 1]
        seen["export"] = export
        seen["existed"] = os.path.exists(export)
        return Result()

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    result = runner.attach_gsc(str(tmp_path), b"PK\x03\x04 not really",
                               "Performance on Search.xlsx")

    assert result["ok"]
    assert seen["export"].endswith(".xlsx"), seen["export"]
    assert seen["existed"], "the command was handed nothing to read"
    assert not os.path.exists(seen["export"]), "the upload was left on disk"


def test_the_uploader_takes_a_workbook_a_zip_or_a_csv(app_runner):
    app = app_test().run()
    app.sidebar.radio[0].set_value("Results").run()

    uploaders = list(app.get("file_uploader"))
    assert uploaders, "no uploader on the Results page"
    assert "Excel" in uploaders[0].label
