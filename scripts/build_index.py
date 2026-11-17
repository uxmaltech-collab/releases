#!/usr/bin/env python3
"""Build releases.json, the public release index of uxmaltech-collab, from local checkouts.

Run with `python3 -I scripts/build_index.py --root <dir with the checkouts>` after `git fetch` in each
checkout. It reads only what is published on each remote default branch (`origin/HEAD`) and its tags, never a
working tree, and writes per release only: version, date, changelog (and, for SoT components, the commit the
release row names, so a lock that pins a commit maps to its version).

- SoT components: every directory of the SoT with a `version.json` and a release table in its README
  (`| x.y.z | `commit` | text |`). Date: the commit date of the row's commit; the latest release takes the
  `released` of version.json when its row has none.
- Repositories (typed SREs and other runtime components): every `vX.Y.Z` tag. Date: the tag's date.
  Changelog: the `## X.Y.Z` section of CHANGELOG.md on the default branch, else the tag message.

What cannot be read is reported on stderr and skipped; the index is still written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

ROW = re.compile(r"^\|\s*`?v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)`?\s*\|\s*`?([0-9a-f]{7,40})?`?\s*\|\s*(.*?)\s*\|\s*$")
TAG = re.compile(r"^v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$")


def git(repo: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


def warn(text: str) -> None:
    print(f"warning: {text}", file=sys.stderr)


def key(version: str) -> tuple:
    core, _, pre = version.partition("-")
    return tuple(int(p) for p in core.split("+")[0].split(".")) + ((1,) if not pre else (0, pre))


def checkout(root: Path, repository: str) -> Path | None:
    path = root / repository.split("/", 1)[1]
    if git(path, "rev-parse", "--verify", "-q", "origin/HEAD") is None:
        warn(f"{repository}: no checkout with origin/HEAD at {path}")
        return None
    return path


def sot_components(repository: str, path: Path) -> dict:
    out = {}
    files = (git(path, "ls-tree", "-r", "--name-only", "origin/HEAD") or "").splitlines()
    for vfile in sorted(f for f in files if f.endswith("/version.json")):
        component = vfile[: -len("/version.json")]
        try:
            meta = json.loads(git(path, "show", f"origin/HEAD:{vfile}") or "")
        except json.JSONDecodeError:
            warn(f"{component}: version.json is not JSON")
            continue
        if not isinstance(meta, dict) or not isinstance(meta.get("version"), str):
            continue
        if meta.get("component") not in (None, component):
            continue  # a schema version file, not a component's
        readme = git(path, "show", f"origin/HEAD:{component}/README.md") or ""
        releases = {}
        for line in readme.splitlines():
            m = ROW.match(line)
            if not m or m.group(1) in releases:
                continue
            version, commit, text = m.groups()
            date = None
            if commit:
                date = (git(path, "show", "-s", "--format=%cs", commit) or "").strip() or None
            releases[version] = {"version": version, "date": date, "commit": commit, "changelog": text.strip()}
        latest = meta["version"]
        if latest not in releases:
            releases[latest] = {"version": latest, "date": None, "commit": None, "changelog": ""}
        if releases[latest]["date"] is None and isinstance(meta.get("released"), str):
            releases[latest]["date"] = meta["released"]
        out[component] = {"repository": repository, "kind": "sot", "latest": latest,
                          "releases": sorted(releases.values(), key=lambda r: key(r["version"]), reverse=True)}
    return out


def changelog_sections(text: str) -> dict:
    sections, current, lines = {}, None, []
    for line in text.splitlines():
        m = re.match(r"^##\s+\[?v?(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\]?", line)
        if m or line.startswith("## "):
            if current:
                sections.setdefault(current, "\n".join(lines).strip())
            current, lines = (m.group(1) if m else None), []
        elif current:
            lines.append(line)
    if current:
        sections.setdefault(current, "\n".join(lines).strip())
    return sections


def repository_component(repository: str, path: Path) -> dict | None:
    tags = (git(path, "tag", "-l", "v*", "--format=%(refname:short)\t%(creatordate:short)") or "").splitlines()
    sections = changelog_sections(git(path, "show", "origin/HEAD:CHANGELOG.md") or "")
    releases = []
    for row in tags:
        name, _, date = row.partition("\t")
        m = TAG.match(name)
        if not m:
            continue
        version = m.group(1)
        text = sections.get(version)
        if text is None:
            text = (git(path, "tag", "-l", "--format=%(contents)", name) or "").strip()
        releases.append({"version": version, "date": date or None, "changelog": text})
    if not releases:
        warn(f"{repository}: no vX.Y.Z tag")
        return None
    releases.sort(key=lambda r: key(r["version"]), reverse=True)
    return {"repository": repository, "kind": "repository", "latest": releases[0]["version"], "releases": releases}


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=here.parent, help="directory holding the checkouts (default: next to this repo)")
    parser.add_argument("--sources", type=Path, default=here / "sources.json")
    parser.add_argument("--out", type=Path, default=here / "releases.json")
    args = parser.parse_args()
    sources = json.loads(args.sources.read_text())
    components = {}
    sot = checkout(args.root, sources["sot"])
    if sot is not None:
        components.update(sot_components(sources["sot"], sot))
    for repository in sources["repositories"]:
        path = checkout(args.root, repository)
        entry = repository_component(repository, path) if path is not None else None
        if entry is not None:
            components[repository] = entry
    index = {"schema": "uxmaltech-collab.releases/v1",
             "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
             "components": dict(sorted(components.items()))}
    args.out.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n")
    print(f"{args.out}: {len(components)} components, {sum(len(c['releases']) for c in components.values())} releases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
