#!/usr/bin/env python3
"""Build releases.json, the public release index of uxmaltech-collab (schema v2), from local checkouts.

Run with `python3 -I scripts/build_index.py --root <dir with the checkouts>` after `git fetch` in each
checkout. It reads only what is published on each remote default branch (`origin/HEAD`) and its tags, never a
working tree, plus the manifests already published under `artifacts/`.

Per release it writes: version, date, changelog, commit (v1 fields, unchanged) and, new in v2: `class`,
`tag_commit`, `artifacts` (name, sha256, bytes, tree_digest for tree tars) and `requires`.

- SoT components: every directory of the SoT with a `version.json` and a release table in its README
  (`| x.y.z | `commit` | text |` or `| x.y.z | tag `name` | text |`). Date: the commit date of the row's commit
  (a tag row: the tag's commit); the latest release takes the `released` of version.json when its row has none.
- Repositories (typed SREs, PHP packages, runtime and other components): every `vX.Y.Z` tag. Date: the tag's
  date. Changelog: the `## X.Y.Z` section of CHANGELOG.md on the default branch, else the tag message. A
  version with a published manifest and no tag (a repository that does not tag) is a release too.

What cannot be read is reported on stderr and skipped; the index is still written. A source whose checkout is
missing keeps the entries the previous index had for it, so a partial run never loses history.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA = "uxmaltech-collab.releases/v2"
SOURCES_SCHEMAS = ("uxmaltech-collab.release-sources/v1", "uxmaltech-collab.release-sources/v2")
CLASSES = ("php-package", "runtime", "layer", "sot-component", "image-recipe", "service")
DEFAULT_REPOSITORY_CLASS = "layer"  # what every repository of a v1 sources.json was
VERSION = r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?"
# | 0.1.2 | `abc1234` | text |      a commit
# | 0.1.2 | tag `x-v0.1.2` | text |   a tag of the SoT repository (the commit is the tag's)
# | 0.1.2 |  | text |               no commit
ROW = re.compile(
    rf"^\|\s*`?v?({VERSION})`?\s*\|\s*(?:(?i:tag)\s+`?([^`\s|]+)`?|`?([0-9a-f]{{7,40}})?`?)\s*\|\s*(.*?)\s*\|\s*$")
TAG = re.compile(rf"^v?({VERSION})$")


def git(repo: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


def warn(text: str) -> None:
    print(f"warning: {text}", file=sys.stderr)


def key(version: str) -> tuple:
    core, _, pre = version.partition("-")
    return tuple(int(p) for p in core.split("+")[0].split(".")) + ((1,) if not pre else (0, pre))


def slug(component: str) -> str:
    """The folder name of a component under `artifacts/<class>/`: its key with `/` as `__`."""
    return component.replace("/", "__")


def release_dir(artifacts: Path, cls: str, component: str, version: str) -> Path:
    return artifacts / cls / slug(component) / version


def sot_class(component: str) -> str:
    """Recipes of images live under `containers/` in the SoT; everything else there is a SoT component."""
    return "image-recipe" if component.startswith("containers/") else "sot-component"


def load_sources(path: Path) -> dict:
    """`{sot, repositories, classes}`. A v1 file (no `classes`) reads as it always did: layers."""
    raw = json.loads(path.read_text())
    if raw.get("schema") not in SOURCES_SCHEMAS:
        warn(f"{path}: unknown sources schema {raw.get('schema')!r}; reading it as v2")
    classes = raw.get("classes") or {}
    for repository, cls in classes.items():
        if cls not in CLASSES:
            warn(f"{path}: {repository}: unknown class {cls!r}")
    return {"sot": raw.get("sot"), "repositories": list(raw.get("repositories", [])), "classes": dict(classes)}


def repository_class(sources: dict, repository: str) -> str:
    return sources["classes"].get(repository, DEFAULT_REPOSITORY_CLASS)


def checkout(root: Path, repository: str) -> Path | None:
    path = root / repository.split("/", 1)[1]
    if git(path, "rev-parse", "--verify", "-q", "origin/HEAD") is None:
        warn(f"{repository}: no checkout with origin/HEAD at {path}")
        return None
    return path


def tag_commit(path: Path, name: str) -> str | None:
    out = git(path, "rev-parse", "-q", "--verify", f"refs/tags/{name}^{{commit}}")
    return out.strip() if out else None


def commit_date(path: Path, commit: str) -> str | None:
    return (git(path, "show", "-s", "--format=%cs", commit) or "").strip() or None


def sot_releases(path: Path, component: str) -> dict:
    """The rows of one SoT component's release table: `{version: release}` (a dict, first row of a version wins)."""
    readme = git(path, "show", f"origin/HEAD:{component}/README.md") or ""
    releases = {}
    for line in readme.splitlines():
        m = ROW.match(line)
        if not m or m.group(1) in releases:
            continue
        version, tag, commit, text = m.groups()
        row_tag = None
        if tag:
            row_tag = tag
            commit = tag_commit(path, tag)
            if commit is None:
                warn(f"{component} {version}: the row names tag {tag!r}, which this checkout does not have")
        date = commit_date(path, commit) if commit else None
        releases[version] = {"version": version, "date": date, "changelog": text.strip(), "commit": commit,
                             "tag": row_tag, "tag_commit": commit if row_tag else None}
    return releases


def sot_components(repository: str, path: Path, only: str | None = None) -> dict:
    out = {}
    files = (git(path, "ls-tree", "-r", "--name-only", "origin/HEAD") or "").splitlines()
    for vfile in sorted(f for f in files if f.endswith("/version.json")):
        component = vfile[: -len("/version.json")]
        if only is not None and component != only:
            continue
        try:
            meta = json.loads(git(path, "show", f"origin/HEAD:{vfile}") or "")
        except json.JSONDecodeError:
            warn(f"{component}: version.json is not JSON")
            continue
        if not isinstance(meta, dict) or not isinstance(meta.get("version"), str):
            continue
        if meta.get("component") not in (None, component):
            continue  # a schema version file, not a component's
        releases = sot_releases(path, component)
        latest = meta["version"]
        if latest not in releases:
            releases[latest] = {"version": latest, "date": None, "changelog": "", "commit": None,
                                "tag": None, "tag_commit": None}
        if releases[latest]["date"] is None and isinstance(meta.get("released"), str):
            releases[latest]["date"] = meta["released"]
        cls = sot_class(component)
        for release in releases.values():
            release.pop("tag")
            release["class"] = cls
        out[component] = {"repository": repository, "kind": "sot", "class": cls, "latest": latest,
                          "releases": sorted(releases.values(), key=lambda r: key(r["version"]), reverse=True)}
    return out


def changelog_sections(text: str) -> dict:
    sections, current, lines = {}, None, []
    for line in text.splitlines():
        m = re.match(rf"^##\s+\[?v?({VERSION})\]?", line)
        if m or line.startswith("## "):
            if current:
                sections.setdefault(current, "\n".join(lines).strip())
            current, lines = (m.group(1) if m else None), []
        elif current:
            lines.append(line)
    if current:
        sections.setdefault(current, "\n".join(lines).strip())
    return sections


def repository_component(repository: str, path: Path | None, cls: str, artifacts: Path | None = None) -> dict | None:
    releases = {}
    if path is not None:
        rows = (git(path, "tag", "-l", "v*", "--format=%(refname:short)\t%(creatordate:short)") or "").splitlines()
        sections = changelog_sections(git(path, "show", "origin/HEAD:CHANGELOG.md") or "")
        for row in rows:
            name, _, date = row.partition("\t")
            m = TAG.match(name)
            if not m:
                continue
            version = m.group(1)
            text = sections.get(version)
            if text is None:
                text = (git(path, "tag", "-l", "--format=%(contents)", name) or "").strip()
            commit = tag_commit(path, name)
            releases[version] = {"version": version, "date": date or None, "changelog": text, "commit": commit,
                                 "class": cls, "tag_commit": commit}
    # A published release the repository has no tag for (it does not tag): the manifest is its row.
    if artifacts is not None:
        base = artifacts / cls / slug(repository)
        for manifest_file in sorted(base.glob("*/MANIFEST.json")) if base.is_dir() else []:
            manifest = read_manifest(manifest_file)
            version = manifest.get("version") if manifest else None
            if isinstance(version, str) and version not in releases:
                commit = manifest.get("commit") if isinstance(manifest.get("commit"), str) else None
                releases[version] = {"version": version, "date": manifest.get("date"), "changelog": "",
                                     "commit": commit, "class": cls, "tag_commit": manifest.get("tag_commit")}
    if not releases:
        warn(f"{repository}: no vX.Y.Z tag and no published manifest")
        return None
    ordered = sorted(releases.values(), key=lambda r: key(r["version"]), reverse=True)
    return {"repository": repository, "kind": "repository", "class": cls, "latest": ordered[0]["version"],
            "releases": ordered}


def read_manifest(path: Path) -> dict | None:
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def attach_manifests(components: dict, artifacts: Path) -> None:
    """Fill `artifacts` and `requires` (and `package`) of every release from its MANIFEST.json, else empty."""
    for component_key, entry in components.items():
        for release in entry["releases"]:
            manifest = read_manifest(release_dir(artifacts, release["class"], component_key, release["version"]) / "MANIFEST.json")
            listed = []
            for artifact in (manifest or {}).get("artifacts", []):
                if isinstance(artifact, dict) and isinstance(artifact.get("name"), str):
                    listed.append({k: artifact[k] for k in ("name", "sha256", "bytes", "tree_digest") if k in artifact})
            release["artifacts"] = listed
            release["requires"] = (manifest or {}).get("requires") if isinstance((manifest or {}).get("requires"), list) else []
            if manifest and isinstance(manifest.get("package"), str):
                release["package"] = manifest["package"]


def upgrade_entry(component_key: str, entry: dict, artifacts: Path) -> dict:
    """A previous (v1 or v2) entry read as v2: the fields v2 adds are filled when missing, nothing else changes."""
    cls = entry.get("class") or (sot_class(component_key) if entry.get("kind") == "sot" else DEFAULT_REPOSITORY_CLASS)
    entry = {**entry, "class": cls}
    entry["releases"] = [{**r, "class": r.get("class") or cls, "tag_commit": r.get("tag_commit")} for r in entry["releases"]]
    attach_manifests({component_key: entry}, artifacts)
    return entry


def read_component(root: Path, sources: dict, component: str, artifacts: Path) -> dict | None:
    """The fresh index entry of one component (a repository `owner/repo` or a SoT path), or None."""
    if component in sources["repositories"]:
        path = checkout(root, component)
        entry = repository_component(component, path, repository_class(sources, component), artifacts)
    elif sources["sot"]:
        path = checkout(root, sources["sot"])
        entry = sot_components(sources["sot"], path, only=component).get(component) if path is not None else None
    else:
        return None
    if entry is not None:
        attach_manifests({component: entry}, artifacts)
    return entry


def build(root: Path, sources: dict, artifacts: Path, previous: dict | None = None) -> dict:
    """The `components` of the index. `previous` is the old `components`: entries of a source that cannot be read
    are kept from it."""
    previous = previous or {}
    fresh, kept = {}, {}
    if sources["sot"]:
        sot = checkout(root, sources["sot"])
        if sot is not None:
            fresh.update(sot_components(sources["sot"], sot))
        else:
            for k, entry in previous.items():
                if entry.get("kind") == "sot" and entry.get("repository") == sources["sot"]:
                    kept[k] = entry
    for repository in sources["repositories"]:
        path = checkout(root, repository)
        entry = repository_component(repository, path, repository_class(sources, repository), artifacts)
        if entry is not None:
            fresh[repository] = entry
        elif path is None and repository in previous:
            kept[repository] = previous[repository]
    for k in kept:
        warn(f"{k}: kept from the previous index")
    attach_manifests(fresh, artifacts)
    components = {**fresh, **{k: upgrade_entry(k, e, artifacts) for k, e in kept.items()}}
    return dict(sorted(components.items()))


def read_index(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    comps = data.get("components") if isinstance(data, dict) else None
    return comps if isinstance(comps, dict) else {}


def write_index(path: Path, components: dict) -> None:
    index = {"schema": SCHEMA,
             "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
             "components": dict(sorted(components.items()))}
    path.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n")


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=here.parent, help="directory holding the checkouts (default: next to this repo)")
    parser.add_argument("--sources", type=Path, default=here / "sources.json")
    parser.add_argument("--artifacts", type=Path, default=here / "artifacts")
    parser.add_argument("--out", type=Path, default=here / "releases.json")
    args = parser.parse_args()
    components = build(args.root, load_sources(args.sources), args.artifacts, read_index(args.out))
    write_index(args.out, components)
    print(f"{args.out}: {len(components)} components, {sum(len(c['releases']) for c in components.values())} releases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
