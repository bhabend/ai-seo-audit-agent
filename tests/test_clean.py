"""The clean command. The rule it must never break: keep the newest run."""

import os
import time

from seo_audit.clean import find_runs, main, select_for_deletion


def make_runs(root, host, names):
    """Create run folders, oldest first, with distinct mtimes."""
    paths = []
    for index, name in enumerate(names):
        path = os.path.join(str(root), host, name)
        os.makedirs(path)
        with open(os.path.join(path, "raw_crawl.csv"), "w") as fh:
            fh.write("url\n" + f"https://{host}/\n" * 10)
        stamp = time.time() - (len(names) - index) * 86400
        os.utime(path, (stamp, stamp))
        paths.append(path)
    return paths


def test_find_runs_lists_newest_first(tmp_path):
    make_runs(tmp_path, "a.com", ["20250101-000000", "20250301-000000"])
    runs = find_runs(str(tmp_path))
    assert [r.name for r in runs["a.com"]] == ["20250301-000000",
                                               "20250101-000000"]
    assert all(r.size_bytes > 0 for r in runs["a.com"])


def test_delete_keeps_the_newest_run(tmp_path):
    old, mid, new = make_runs(tmp_path, "a.com",
                              ["20250101-000000", "20250201-000000",
                               "20250301-000000"])
    code = main(["--root", str(tmp_path), "--delete", "a.com", "--yes"])
    assert code == 0
    assert os.path.isdir(new)
    assert not os.path.isdir(old)
    assert not os.path.isdir(mid)


def test_delete_all_still_keeps_the_newest_run(tmp_path):
    old, new = make_runs(tmp_path, "a.com",
                         ["20250101-000000", "20250301-000000"])
    main(["--root", str(tmp_path), "--delete-all", "a.com", "--yes"])
    assert os.path.isdir(new)
    assert not os.path.isdir(old)


def test_a_lone_run_is_never_deleted(tmp_path):
    only, = make_runs(tmp_path, "a.com", ["20250101-000000"])
    main(["--root", str(tmp_path), "--delete-all", "a.com", "--yes"])
    assert os.path.isdir(only)


def test_older_than_spans_hosts_but_spares_each_newest(tmp_path):
    a_old, a_new = make_runs(tmp_path, "a.com",
                             ["20250101-000000", "20250102-000000"])
    b_old, b_new = make_runs(tmp_path, "b.com",
                             ["20250101-000000", "20250102-000000"])
    # Everything here is far older than a day, yet each newest survives.
    main(["--root", str(tmp_path), "--older-than", "0.5", "--yes"])
    assert os.path.isdir(a_new) and os.path.isdir(b_new)
    assert not os.path.isdir(a_old) and not os.path.isdir(b_old)


def test_selection_never_includes_a_newest_run(tmp_path):
    make_runs(tmp_path, "a.com", ["20250101-000000", "20250301-000000"])
    runs = find_runs(str(tmp_path))
    newest = {host_runs[0].path for host_runs in runs.values()}
    for kwargs in ({"host": "a.com"}, {"host": "a.com", "delete_all": True},
                   {"older_than_days": 0.0}):
        doomed = {r.path for r in select_for_deletion(runs, **kwargs)}
        assert not (doomed & newest)


def test_confirmation_is_required_without_yes(tmp_path):
    old, new = make_runs(tmp_path, "a.com",
                         ["20250101-000000", "20250301-000000"])
    main(["--root", str(tmp_path), "--delete", "a.com"], input_fn=lambda _: "n")
    assert os.path.isdir(old) and os.path.isdir(new)

    main(["--root", str(tmp_path), "--delete", "a.com"], input_fn=lambda _: "y")
    assert not os.path.isdir(old)
    assert os.path.isdir(new)


def test_listing_runs_deletes_nothing(tmp_path, capsys):
    old, new = make_runs(tmp_path, "a.com",
                         ["20250101-000000", "20250301-000000"])
    main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "a.com" in out and "newest" in out
    assert os.path.isdir(old) and os.path.isdir(new)


def test_unknown_host_is_an_error(tmp_path, capsys):
    make_runs(tmp_path, "a.com", ["20250101-000000"])
    assert main(["--root", str(tmp_path), "--delete", "nope.com", "--yes"]) == 1
