"""Housekeeping for output/: python -m seo_audit.clean

Runs accumulate one folder each. This lists them, and deletes old ones on
request. The newest run for a host is never deleted, whatever is asked, so
there is always something to compare the next run against.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

DEFAULT_ROOT = "output"


@dataclass
class Run:
    host: str
    name: str
    path: str
    size_bytes: int
    mtime: float

    @property
    def size_kb(self) -> int:
        return round(self.size_bytes / 1024)

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime))


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def find_runs(root: str = DEFAULT_ROOT) -> Dict[str, List[Run]]:
    """Runs per host, newest first."""
    runs: Dict[str, List[Run]] = {}
    if not os.path.isdir(root):
        return runs
    for host in sorted(os.listdir(root)):
        host_dir = os.path.join(root, host)
        if not os.path.isdir(host_dir):
            continue
        found = []
        for name in os.listdir(host_dir):
            path = os.path.join(host_dir, name)
            if not os.path.isdir(path):
                continue
            found.append(Run(host=host, name=name, path=path,
                             size_bytes=dir_size(path),
                             mtime=os.path.getmtime(path)))
        if found:
            runs[host] = sorted(found, key=lambda r: r.mtime, reverse=True)
    return runs


def format_listing(runs: Dict[str, List[Run]]) -> str:
    if not runs:
        return "No runs found."
    lines = []
    grand_total = 0
    for host, host_runs in runs.items():
        total = sum(r.size_bytes for r in host_runs)
        grand_total += total
        lines.append(f"{host}  ({len(host_runs)} run(s), "
                     f"{round(total / 1024)} KB)")
        for index, run in enumerate(host_runs):
            marker = "newest" if index == 0 else "      "
            lines.append(f"  {marker}  {run.name}  {run.size_kb:>7} KB  "
                         f"{run.when}")
    lines.append(f"\nTotal: {round(grand_total / 1024)} KB")
    return "\n".join(lines)


def select_for_deletion(runs: Dict[str, List[Run]], host: Optional[str] = None,
                        delete_all: bool = False,
                        older_than_days: Optional[float] = None) -> List[Run]:
    """Which runs to remove. The newest run per host is always excluded."""
    doomed: List[Run] = []
    cutoff = (time.time() - older_than_days * 86400
              if older_than_days is not None else None)

    for host_name, host_runs in runs.items():
        if host is not None and host_name != host:
            continue
        # [0] is the newest and is never a candidate.
        candidates = host_runs[1:]
        if cutoff is not None:
            candidates = [r for r in candidates if r.mtime < cutoff]
        elif not (delete_all or host is not None):
            candidates = []
        doomed.extend(candidates)
    return doomed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seo-audit-clean",
        description="List and prune crawl run folders under output/.",
    )
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--delete", metavar="HOST", default=None,
                        help="Delete all but the newest run for HOST.")
    parser.add_argument("--delete-all", metavar="HOST", default=None,
                        help="Delete every run for HOST except the newest.")
    parser.add_argument("--delete-host", metavar="HOST", default=None,
                        help="Delete EVERY run for HOST, the newest one "
                             "included, and the host folder with them. The "
                             "only path that does not keep a run, so it "
                             "needs --yes.")
    parser.add_argument("--older-than", metavar="DAYS", type=float, default=None,
                        help="Delete runs older than DAYS across all hosts, "
                             "always keeping the newest per host.")
    parser.add_argument("--yes", action="store_true",
                        help="Do not ask for confirmation.")
    return parser


def _remove_host(runs: Dict[str, List[Run]], host: str, root: str,
                 yes: bool) -> int:
    """Every run for one host, the newest included, and the folder itself.

    The only path in this file that leaves a host with nothing. It is not
    offered behind a y/N prompt: whoever asks for it says --yes on the
    command line, so it can never happen by leaning on the return key.
    """
    doomed = runs.get(host, [])
    total_kb = round(sum(r.size_bytes for r in doomed) / 1024)
    print(f"About to delete ALL {len(doomed)} run(s) for {host}, "
          f"{total_kb} KB, including the newest:")
    for run in doomed:
        print(f"  {run.host}/{run.name}  {run.size_kb} KB  {run.when}")
    if not yes:
        print("Refusing without --yes: this is the one delete that keeps "
              "nothing.", file=sys.stderr)
        return 1

    for run in doomed:
        shutil.rmtree(run.path, ignore_errors=True)
    host_dir = os.path.join(root, host)
    try:
        if os.path.isdir(host_dir) and not os.listdir(host_dir):
            os.rmdir(host_dir)
    except OSError:
        pass
    print(f"Deleted {len(doomed)} run(s), {total_kb} KB freed. "
          f"{host} is gone.")
    return 0


def main(argv=None, input_fn=input) -> int:
    args = build_parser().parse_args(argv)
    runs = find_runs(args.root)

    host = args.delete or args.delete_all or args.delete_host
    if host is None and args.older_than is None:
        print(format_listing(runs))
        return 0

    if host is not None and host not in runs:
        print(f"No runs found for host {host!r} under {args.root}/",
              file=sys.stderr)
        return 1

    if args.delete_host:
        return _remove_host(runs, args.delete_host, args.root, args.yes)

    doomed = select_for_deletion(runs, host=host,
                                 delete_all=args.delete_all is not None,
                                 older_than_days=args.older_than)
    if not doomed:
        print("Nothing to delete: the newest run per host is always kept.")
        return 0

    total_kb = round(sum(r.size_bytes for r in doomed) / 1024)
    print(f"About to delete {len(doomed)} run(s), {total_kb} KB:")
    for run in doomed:
        print(f"  {run.host}/{run.name}  {run.size_kb} KB  {run.when}")

    if not args.yes:
        answer = input_fn("Delete these? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing deleted.")
            return 0

    for run in doomed:
        shutil.rmtree(run.path, ignore_errors=True)
    print(f"Deleted {len(doomed)} run(s), {total_kb} KB freed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
