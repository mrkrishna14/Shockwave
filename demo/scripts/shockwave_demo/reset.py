"""``reset``: delete ONLY generated demo state.

Removed: the work directory's generated entries (``repos/``, ``runs/``, ``snapshots.json``,
``live-check/``; default ``demo/.work``) and the reports directory's generated entries
(``results/``, ``runs/``, ``sources/``; default ``demo/reports``). The checked-in
``demo/reports/README.md`` and ``.gitkeep`` stay. Never touched: templates, fixtures,
demo/expected (including expected/results), the lock, manifests, migrations and the
fetched-source cache (demo/.cache). Only these known names are deleted, so an override
directory never loses anything this tool did not write.
"""

from __future__ import annotations

from pathlib import Path

from shockwave_demo.common import Layout, display_path, remove_tree

WORK_ENTRIES = ("repos", "runs", "snapshots.json", "live-check")
REPORT_ENTRIES = ("results", "runs", "sources")
KEPT_REPORT_FILES = ("README.md", ".gitkeep")


def generated_paths(layout: Layout) -> list[Path]:
    paths = [layout.work_dir / name for name in WORK_ENTRIES]
    paths += [layout.reports_dir / name for name in REPORT_ENTRIES]
    return [path for path in paths if path.is_symlink() or path.exists()]


def run_reset(layout: Layout, *, dry_run: bool = False) -> list[str]:
    targets = generated_paths(layout)
    verb = "would delete" if dry_run else "deleted"
    lines = [
        "Reset generated demo state (templates, fixtures, expected results, lock, manifests and migrations are kept)",
        f"  $WORK_DIR = {display_path(layout.work_dir)}, $REPORTS_DIR = {display_path(layout.reports_dir)}",
    ]
    for path in targets:
        if not dry_run:
            remove_tree(path, inside=path.parent)
        lines.append(f"  {verb}  {display_path(path, layout)}")
    if not dry_run and layout.work_dir.is_dir() and not any(layout.work_dir.iterdir()):
        layout.work_dir.rmdir()
    if not targets:
        lines.append("  nothing to delete")
    kept = [name for name in KEPT_REPORT_FILES if (layout.reports_dir / name).exists()]
    if kept:
        lines.append(f"  kept     {', '.join(kept)} in {display_path(layout.reports_dir, layout)}")
    return lines
