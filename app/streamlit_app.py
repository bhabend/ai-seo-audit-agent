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

REFRESH_SECONDS = 5
PAGES = ("Run audit", "Results", "Report", "Runs")


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
        started = st.form_submit_button("Start audit")

    if started:
        if not domain.strip():
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
            st.session_state["log"] = launch["log"]
            st.session_state["started"] = launch["started"]
            st.session_state["run_dir"] = None
            st.success(f"Started as process {launch['pid']}. "
                       f"Log: {launch['log']}")

    log_path = st.session_state.get("log")
    if not log_path:
        # A run may have been started before this console was restarted.
        attached = runner.attach()
        if attached:
            st.session_state["log"] = log_path = attached["log"]
            st.session_state["run_dir"] = attached["run_dir"]
            st.info(f"Attached to the run already in flight: {log_path}")

    if not log_path:
        st.write("No run in flight.")
        return

    run_dir = (st.session_state.get("run_dir")
               or runner.run_dir_of(log_path)
               or runner.newest_run_dir(
                   since=st.session_state.get("started", 0.0)))
    if run_dir:
        st.session_state["run_dir"] = run_dir

    status = runner.poll(run_dir, log_path)
    st.subheader(f"Stage: {status['stage']}")
    if status["run_dir"]:
        st.caption(f"Writing to {status['run_dir']}")
    columns = st.columns(3)
    for column, (label, value) in zip(columns, status["counters"].items()):
        column.metric(label.title(), value)

    if status["failed"]:
        st.error(f"The run stopped without findings. "
                 f"Exit code {status['exit_code']}.")
    elif status["done"]:
        st.success("Finished. The Results and Report pages can read it now.")

    st.text_area("Log", status["tail"], height=220)
    if status["running"]:
        st.caption(f"Refreshing every {REFRESH_SECONDS} seconds.")
        _autorefresh()


def _autorefresh():
    """Ask Streamlit to come back in a few seconds, if it knows how."""
    fragment = getattr(st, "autorefresh", None)
    if callable(fragment):  # pragma: no cover - Streamlit version dependent
        fragment(interval=REFRESH_SECONDS * 1000, key="poll")
    else:
        st.button("Refresh now", key="refresh")


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
    for key in ("crawlability", "indexability_technical", "on_page",
                "content", "schema", "links", "performance"):
        section = findings.get(key)
        if not section:
            continue
        with st.expander(key.replace("_", " ").title()):
            st.dataframe(_section_rows(section), use_container_width=True)

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

    st.subheader("Files")
    for name in runner.RUN_FILES:
        path = os.path.join(run["path"], name)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as handle:
            st.download_button(name, handle.read(), file_name=name,
                               key=f"dl_{name}")


def _section_rows(section, prefix=""):
    """A findings section flattened to name and value, shares said in words."""
    rows = []
    if isinstance(section, dict):
        for key, value in section.items():
            name = f"{prefix}{key.replace('_', ' ')}"
            if isinstance(value, dict) and "count" in value:
                rows.append({"Measure": name, "Value": _share(value)})
            elif isinstance(value, (int, float, str, bool)) or value is None:
                rows.append({"Measure": name, "Value": value})
            elif isinstance(value, list):
                rows.append({"Measure": name, "Value": f"{len(value)} listed"})
            elif isinstance(value, dict):
                rows.extend(_section_rows(value, prefix=f"{name}: "))
    return rows


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
    if st.button(f"Generate {fmt} report"):
        with st.spinner("Building the document..."):
            result = runner.generate_report(run["path"], fmt)
        st.session_state["report"] = result
        if result["ok"]:
            st.success("Report written and validated.")
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
    } for host, items in runs.items() for item in items], use_container_width=True)

    st.subheader("Delete")
    st.caption("The newest run of a host is kept unless you delete all of "
               "them. Deleting cannot be undone.")
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


main()
