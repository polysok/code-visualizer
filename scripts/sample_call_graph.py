#!/usr/bin/env python3
"""Sample functions from an indexed repository for a manual call-graph audit.

Writes a Markdown checklist: for N random functions, every caller and callee
the graph knows (with file:line), so a reviewer can compare each one with the
IDE's "Find usages" / "Call hierarchy" and tick it as correct, false positive,
or note what is missing. The summary at the end gives the share of heuristic
edges, the ones most likely to be wrong.

Usage:
    python scripts/sample_call_graph.py /path/to/repo -n 20 --seed 1 -o audit.md

Run it from an environment where `cviz` works (same context/database).
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

# Run from a source checkout without installing the package.
_SRC = Path(__file__).resolve().parent.parent / "src"
if (_SRC / "code_visualizer").is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repo", help="Indexed repository root")
    parser.add_argument("-n", type=int, default=20, help="Number of functions (default 20)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for a reproducible sample")
    parser.add_argument("-o", "--output", default="call-graph-audit.md", help="Markdown file to write")
    parser.add_argument("--context", default=None, help="CViz context to use")
    args = parser.parse_args()

    from code_visualizer.cli.cli_helpers import _initialize_services
    from code_visualizer.viz import neighborhood as nb

    repo = Path(args.repo).resolve().as_posix().rstrip("/") + "/"
    db_manager = _initialize_services(args.context)[0]
    if db_manager is None:
        print("Could not open the CViz database", file=sys.stderr)
        return 1
    rng = random.Random(args.seed)
    try:
        with db_manager.get_driver().session() as session:
            rows = session.run(
                "MATCH (f:Function) WHERE f.path STARTS WITH $repo AND f.name <> '<module>' "
                "RETURN f.name AS name, f.path AS path, f.line_number AS line",
                repo=repo,
            ).data()
            if not rows:
                print(f"No functions indexed under {repo}", file=sys.stderr)
                return 1
            sample = rng.sample(rows, min(args.n, len(rows)))
            sample.sort(key=lambda r: (r["path"], r["line"] or 0))

            lines = [
                f"# Call-graph audit — {repo}",
                "",
                f"{len(sample)} functions sampled out of {len(rows)} (seed {args.seed}).",
                "For each edge, tick ✅ if the IDE confirms it, ❌ if it is wrong; add missing ones by hand.",
                "`~` marks heuristic edges (resolved by name only).",
                "",
            ]
            total = heuristic = 0
            for row in sample:
                data = nb.neighborhood(session, path=row["path"], name=row["name"], line=row["line"],
                                       kinds=["CALLS"], per_node_limit=200, max_nodes=500)
                rel = row["path"][len(repo):]
                lines += [f"## `{row['name']}` — {rel}:{row['line']}", ""]
                by_id = {n["id"]: n for n in data["nodes"]}
                cid = data["center"]["id"]
                for title, role, side in (("Callers", "caller", "source"), ("Callees", "callee", "target")):
                    edges = [l for l in data["links"]
                             if (l["target"] if side == "source" else l["source"]) == cid]
                    lines.append(f"**{title}** ({len(edges)})")
                    lines.append("")
                    if not edges:
                        lines.append("- none — [ ] confirmed none in the IDE")
                    for l in edges:
                        other = by_id.get(l[side])
                        if other is None:
                            continue
                        total += 1
                        heuristic += bool(l.get("heuristic"))
                        where = (other.get("path") or "")
                        where = where[len(repo):] if where.startswith(repo) else where
                        call_at = f", call at line {l['line']}" if l.get("line") else ""
                        mark = "~ " if l.get("heuristic") else ""
                        lines.append(f"- [ ] {mark}`{other['label']}` ({where}:{other.get('line')}{call_at})")
                    hidden = data["center"]["hidden"].get(role, 0)
                    if hidden:
                        lines.append(f"- … {hidden} more not listed")
                    lines.append("- missing in graph: ")
                    lines.append("")
            share = (heuristic / total * 100) if total else 0.0
            lines += [
                "## Summary",
                "",
                f"- Edges listed: {total}; heuristic: {heuristic} ({share:.0f} %)",
                "- Correct: __ / __ · False positives: __ · Missing: __",
                "- Threshold to proceed (plan, lot 0): ≥ 90 % correct, no ghost node after a rename.",
                "",
            ]
    finally:
        db_manager.close_driver()

    Path(args.output).write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {args.output} ({len(sample)} functions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
