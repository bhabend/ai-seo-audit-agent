"""The operator console: four pages over the three commands.

    streamlit run app/streamlit_app.py

Nothing here crawls, scores or writes a report. The Run page starts the
audit command as its own process and then watches the files that run writes;
the Results page reads findings.json; the Report page runs the report
command; the Runs page lists folders and asks the clean command to delete
them. Close the browser mid audit and the audit carries on, because it was
never running inside this process.
"""

from __future__ import annotations

import json
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import runner  # noqa: E402  (the app runs as a script, not a package)

import seo_audit  # noqa: E402
from seo_audit import report  # noqa: E402  (the section tables, as rendered)

REFRESH_SECONDS = 4
PAGES = ("Run audit", "Results", "Report", "Runs")

# Said before the click, not after the bill.
COST_NOTE = ("Long format writes narrative with OpenAI: about ten calls and "
             "about one cent per report. Short format makes no calls.")


def _state(key, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


def _load(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def _share(obj, default="") -> str:
    """A share object as the report writes it: N of M whole."""
    if not isinstance(obj, dict) or "count" not in obj:
        return default
    return f"{obj.get('count', 0)} of {obj.get('whole', 0)} " \
           f"{obj.get('whole_is', '')}".strip()


# --- page 1: run an audit ----------------------------------------------------

def page_run_audit():
    st.header("Run audit")
    st.caption("The audit runs as its own process. You can close this tab.")

    running = runner.running_runs()
    busy = bool(running)
    if busy:
        st.warning(f"A run is going: {running[0]}. One at a time, so the "
                   f"counters below always belong to it.")

    with st.form("audit"):
        domain = st.text_input("Domain", placeholder="https://example.com")
        sitemap = st.text_input("Sitemap URL (optional)", value="")
        columns = st.columns(2)
        max_pages = columns[0].number_input("Max pages", min_value=1,
                                            max_value=100000, value=2000,
                                            step=100)
        workers = columns[1].number_input("Workers", min_value=1,
                                          max_value=16, value=4)
        pagespeed = st.checkbox("Measure speed with PageSpeed Insights",
                                value=True,
                                help="Needs PAGESPEED_API_KEY in .env. "
                                     "Adds a few minutes.")
        compare = st.checkbox("Compare with the previous run of this host",
                              value=True)
        with st.expander("Advanced"):
            sweep_limit = st.number_input("Sitemap sweep limit", min_value=0,
                                          max_value=1000000, value=10000,
                                          step=1000)
            external_check_limit = st.number_input(
                "External link check limit", min_value=0, max_value=100000,
                value=1000, step=100)
        started = st.form_submit_button("Start audit", disabled=busy)

    if started:
        if busy:
            st.warning("A run is already going. Wait for it to finish.")
        elif not domain.strip():
            st.error("A domain is required.")
        else:
            launch = runner.start_audit(domain.strip(), {
                "sitemap": sitemap.strip() or None,
                "max_pages": int(max_pages),
                "workers": int(workers),
                "pagespeed": bool(pagespeed),
                "compare": bool(compare),
                "sweep_limit": int(sweep_limit),
                "external_check_limit": int(external_check_limit),
            })
            if not launch.get("ok"):
                st.error(launch.get("error", "The run was refused."))
            else:
                # The view is bound to the log this click created, so a run
                # someone else starts later cannot walk into this page.
                st.session_state["log"] = launch["log"]
                st.success(f"Started as process {launch['pid']}. "
                           f"Log: {launch['log']}")

    log_path = st.session_state.get("log")
    if not log_path:
        # This console may have been restarted under a run. There is only
        # ever one, so there is nothing to choose between.
        attached = runner.attach()
        if attached:
            st.session_state["log"] = log_path = attached["log"]
            st.info(f"Attached to the run already in flight: {log_path}")

    if not log_path:
        st.write("No run in flight.")
        return

    if runner.exit_code_of(log_path) is None:
        _progress(log_path)
    else:
        # Finished: the same block, drawn once, with no timer behind it.
        _render_progress(log_path)


@st.fragment(run_every=REFRESH_SECONDS)
def _progress(log_path: str) -> None:
    """The progress block, redrawn on a timer while the run is going.

    A fragment rather than a whole page rerun: the form above keeps what was
    typed into it. When the run ends the fragment stops asking for more, and
    the next full rerun draws the finished block without a timer.
    """
    finished = _render_progress(log_path)
    if finished:
        st.rerun()


def _render_progress(log_path: str) -> bool:
    """Draw the bound run's stage, counters and log tail. True when over."""
    run_dir = runner.bound_run_dir(log_path)
    status = runner.poll(run_dir, log_path)

    st.subheader(f"Stage: {status['stage']}")
    st.caption(f"Watching {log_path}")
    if status["run_dir"]:
        st.caption(f"Writing to {status['run_dir']}")
    if status["running"]:
        _live_panel(status)
    else:
        columns = st.columns(3)
        for column, (label, value) in zip(columns,
                                          status["counters"].items()):
            column.metric(label.title(), value)

    if status["failed"]:
        st.error(f"The run stopped without findings. "
                 f"Exit code {status['exit_code']}.")
    elif status["done"]:
        st.success("Finished. The Results and Report pages can read it now.")

    st.text_area("Log", status["tail"], height=220)
    if status["running"]:
        st.caption(f"Refreshing every {REFRESH_SECONDS} seconds.")
        st.button("Refresh now", key="refresh")
    return not status["running"]


def _live_panel(status) -> None:
    """What a run in flight is doing: time so far and its stage's counts.

    Every number is one the run wrote down. The stage above is the last STAGE
    line in its log, the time runs from its STARTED line, and each count is
    rows in the file that holds them. There is no percentage and no time
    remaining: the crawl finds pages as it goes and never knows a total.
    """
    counts = list(status.get("stage_counts") or [])
    with st.container(border=True):
        columns = st.columns(1 + len(counts))
        columns[0].metric("Elapsed",
                          format_elapsed(status.get("elapsed_seconds")))
        for column, (label, value) in zip(columns[1:], counts):
            column.metric(label, value)
        if counts:
            st.caption("Rows the run has written so far. The crawl finds "
                       "pages as it goes, so there is no total to count "
                       "towards.")
        elif status.get("stage") == "starting":
            st.caption("No run folder yet: robots.txt and the homepage are "
                       "fetched first.")
        else:
            st.caption("The run's folder is not known yet, so there are no "
                       "rows to count.")


def format_elapsed(seconds) -> str:
    """Whole seconds as 45s, 2m 05s or 1h 02m."""
    if seconds is None:
        return "n/a"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


# --- page 2: results ---------------------------------------------------------

def _pick_run(key: str):
    runs = runner.list_runs()
    if not runs:
        st.info("No runs yet. Start one on the Run audit page.")
        return None
    host = st.selectbox("Host", sorted(runs), key=f"{key}_host")
    labels = {f"{r['name']}  ({r['when']}, {r['size_kb']} KB)": r
              for r in runs[host]}
    chosen = st.selectbox("Run", list(labels), key=f"{key}_run")
    return labels[chosen]


def page_results():
    st.header("Results")
    run = _pick_run("results")
    if not run:
        return
    findings = _load(os.path.join(run["path"], "findings.json"))
    if not findings:
        st.warning("This run has no findings.json. It did not finish.")
        return

    headline = findings.get("headline") or {}
    columns = st.columns(4)
    columns[0].metric("Site score", headline.get("site_score", "n/a"))
    columns[1].metric("Mean page score", headline.get("mean_page_score",
                                                      "n/a"))
    columns[2].metric("Pages scored", headline.get("pages_scored", 0))
    columns[3].metric("Site level deduction",
                      headline.get("site_level_deduction", 0))
    coverage = (findings.get("meta") or {}).get("coverage") or {}
    st.caption(f"{coverage.get('pages_found', 0)} addresses crawled, "
               f"{coverage.get('pages_parsed', 0)} read as pages, "
               f"{coverage.get('sitemap_urls_total', 0)} in the sitemap. "
               f"Performance component: "
               f"{headline.get('performance_component', 'not included')}.")

    fixes = findings.get("prioritised_fixes") or []
    if fixes:
        st.subheader("Priority fixes")
        st.dataframe([{
            "Fix": fix.get("label", ""),
            "Pages affected": _share(fix.get("pages_affected")),
            "Targets": _share(fix.get("targets"), "n/a"),
            "Severity": fix.get("severity", ""),
            "Scope": fix.get("fix_scope", ""),
        } for fix in fixes], use_container_width=True)

    st.subheader("Sections")
    coverage = (findings.get("meta") or {}).get("coverage") or {}
    for key, heading in report.SECTIONS:
        section = findings.get(key)
        if not section:
            continue
        rows = report.short_section_rows(key, section, coverage)
        with st.expander(f"{heading} ({len(rows) - 1} findings)"):
            st.dataframe([dict(zip(report.SHORT_SECTION_COLUMNS, row))
                          for row in rows], use_container_width=True)

    comparison = findings.get("comparison") or {}
    if comparison.get("previous_run"):
        st.subheader("Compared with the previous run")
        st.caption(f"Previous run: {comparison['previous_run']}. "
                   f"Coverage changed: "
                   f"{bool(comparison.get('coverage_changed'))}.")
        notable = comparison.get("notable") or []
        if notable:
            st.dataframe([{
                "Measure": item.get("metric", "").replace("_", " "),
                "Previous": item.get("previous"),
                "Now": item.get("now"),
                "Change": item.get("change"),
                "Note": item.get("note", ""),
            } for item in notable], use_container_width=True)

    _search_performance(run, findings)

    st.subheader("Files")
    for name in runner.RUN_FILES:
        path = os.path.join(run["path"], name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as handle:
            st.download_button(name, handle.read(), file_name=name,
                               key=f"dl_{name}")


def _search_performance(run, findings):
    """What search engines did with this site, once an export is attached."""
    block = findings.get("search_performance") or {}
    st.subheader("Search performance")

    if block.get("provided"):
        totals = block.get("totals") or {}
        columns = st.columns(3)
        columns[0].metric("Impressions", totals.get("impressions", 0))
        columns[1].metric("Clicks", totals.get("clicks", 0))
        columns[2].metric("Searches", totals.get("queries_in_export", 0))
        st.caption(f"Covering {block.get('date_range')}. "
                   f"{block.get('coverage_note', '')}")
        st.dataframe([dict(zip(report.SEARCH_COVERAGE_COLUMNS, row))
                      for row in report.search_coverage_rows(block)],
                     use_container_width=True)
        issue_rows = report.search_issue_rows(block)
        if issue_rows:
            st.dataframe([dict(zip(report.SHORT_SECTION_COLUMNS, row))
                          for row in issue_rows], use_container_width=True)
        near = report.near_miss_rows(block)
        if near:
            with st.expander(f"Searches the site nearly wins ({len(near)})"):
                st.dataframe([dict(zip(report.NEAR_MISS_COLUMNS, row))
                              for row in near], use_container_width=True)
        cannibal = report.cannibalised_rows(block)
        if cannibal:
            with st.expander(f"Searches answered by more than one page "
                             f"({len(cannibal)})"):
                st.dataframe([dict(zip(report.CANNIBAL_COLUMNS, row))
                              for row in cannibal],
                             use_container_width=True)
    else:
        st.caption("No Search Console export attached to this run yet.")

    upload = st.file_uploader(
        "Attach Search Console export (zip, Excel workbook or CSV)",
        type=["zip", "xlsx", "csv"], key=f"gsc_{run['name']}",
        help="Whatever Search Console gave you: the export zip, the "
             "spreadsheet, or one of its CSVs. It is read, joined to this "
             "crawl and then deleted. Only the audit's own join file is "
             "kept.")
    if upload is not None and st.button("Attach export", key="attach_gsc"):
        with st.spinner("Joining the export to the crawl..."):
            result = runner.attach_gsc(run["path"], upload, upload.name)
        if result["ok"]:
            st.success("Attached. The tables above now include search data.")
            st.code(result["output"][-1500:])
            st.rerun()
        else:
            st.error("The join failed.")
            st.code(result["output"][-1500:])


# --- page 3: report ----------------------------------------------------------

def page_report():
    st.header("Report")
    run = _pick_run("report")
    if not run:
        return
    if not run["complete"]:
        st.warning("This run has no findings.json, so there is nothing to "
                   "report on.")
        return

    fmt = st.radio("Format", ("short", "long"), horizontal=True, key="fmt",
                   help="Short is the client deliverable and makes no model "
                        "call. Long is the narrated version, for internal "
                        "use, and calls the model.")
    st.caption(COST_NOTE)
    mode = runner.run_mode(run["path"])
    st.caption(f"This run is in {mode} mode."
               + ("" if mode == "full" else
                  " Attach a Search Console export on the Results page to "
                  "cover search performance."))
    if st.button(f"Generate {fmt} report"):
        with st.spinner("Building the document..."):
            result = runner.generate_report(run["path"], fmt)
        st.session_state["report"] = result
        if result["ok"]:
            st.success("Report written and validated.")
        elif result.get("message"):
            # A missing key is a setup question, not a crash.
            st.warning(result["message"])
        else:
            st.error("The report command failed.")
            st.code(result["output"][-2000:])

    result = st.session_state.get("report") or {}
    docx = result.get("docx") or _existing_docx(run["path"])
    if docx and os.path.exists(docx):
        with open(docx, "rb") as handle:
            st.download_button(f"Download {os.path.basename(docx)}",
                               handle.read(), file_name=os.path.basename(docx),
                               key="dl_docx")
    usage = runner.report_usage(run["path"])
    if usage:
        st.caption("The long format's last run cost:")
        columns = st.columns(3)
        columns[0].metric("Model calls", usage.get("calls", 0))
        columns[1].metric("Input tokens", usage.get("input_tokens", 0))
        columns[2].metric("Output tokens", usage.get("output_tokens", 0))


def _existing_docx(run_dir):
    for name in sorted(os.listdir(run_dir)):
        if name.lower().endswith(".docx"):
            return os.path.join(run_dir, name)
    return None


# --- page 4: runs ------------------------------------------------------------

def page_runs():
    st.header("Runs")
    runs = runner.list_runs()
    if not runs:
        st.info("No runs on disk.")
        return

    st.dataframe([{
        "Host": host, "Run": item["name"], "When": item["when"],
        "Size (KB)": item["size_kb"], "Files": item["files"],
        "Finished": item["complete"], "Report": item["report"],
        "Search data": item.get("search", False),
    } for host, items in runs.items() for item in items], use_container_width=True)

    stale = runner.stale_runs()
    if stale:
        st.subheader("Stopped without finishing")
        st.caption("These runs never wrote an exit code and have been quiet "
                   "for over a day, so the machine they ran on is gone. They "
                   "no longer block a new run.")
        for log_path in stale:
            with st.expander(os.path.basename(log_path)):
                st.code(runner.poll(runner.run_dir_of(log_path),
                                    log_path)["tail"] or "(empty log)")

    st.subheader("Delete")
    st.caption("The newest run of a host is kept by both delete buttons. "
               "Removing the host is the one action that keeps nothing. "
               "Deleting cannot be undone.")
    for host, items in runs.items():
        st.markdown(f"**{host}** ({len(items)} runs)")
        columns = st.columns(2)
        older = columns[0].checkbox(f"Confirm deleting older runs of {host}",
                                    key=f"c_old_{host}")
        if columns[0].button(f"Delete older runs of {host}",
                             key=f"b_old_{host}", disabled=not older):
            if not older:
                st.warning("Tick the box first.")
            else:
                _report_delete(runner.delete_runs(host, all=False))
        every = columns[1].checkbox(f"Confirm deleting ALL runs of {host}",
                                    key=f"c_all_{host}")
        if columns[1].button(f"Delete all runs of {host}",
                             key=f"b_all_{host}", disabled=not every):
            if not every:
                st.warning("Tick the box first.")
            else:
                _report_delete(runner.delete_runs(host, all=True))

        # Removing the host keeps nothing at all, so a tick is not enough:
        # the name has to be typed, which is hard to do by accident.
        typed = st.text_input(f"Type {host} to remove the host entirely",
                              key=f"t_host_{host}", value="")
        matches = typed.strip() == host
        if st.button(f"Remove {host} entirely", key=f"b_host_{host}",
                     disabled=not matches):
            if not matches:
                st.warning(f"Type {host} exactly to remove it.")
            else:
                _report_delete(runner.delete_host(host))


def _report_delete(result):
    if result["ok"]:
        st.success("Deleted.")
        st.code(result["output"][-1000:])
    else:
        st.error("The clean command refused.")
        st.code(result["output"][-1000:])


# --- the console -------------------------------------------------------------

def main():
    st.set_page_config(page_title="SEO Audit console", layout="wide")
    st.sidebar.title("SEO Audit")
    st.sidebar.caption(f"seo_audit v{seo_audit.__version__}")
    page = st.sidebar.radio("Page", PAGES, key="page")
    st.sidebar.caption("This console only runs the command line tools. "
                       "Audits keep running if you close the browser.")

    if page == "Run audit":
        page_run_audit()
    elif page == "Results":
        page_results()
    elif page == "Report":
        page_report()
    else:
        page_runs()


# Streamlit executes this file as "__main__" on every rerun. The guard keeps
# an ordinary import side effect free, so a test can read this module without
# painting a page half way through somebody else's script run.
if __name__ == "__main__":
    main()
