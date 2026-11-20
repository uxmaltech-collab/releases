#!/usr/bin/env python3
"""Build releases.json, the public release index of a publisher (schema `collab.releases/v2`), from local checkouts.

Run with `python3 -I scripts/build_index.py --root <dir with the checkouts>` after `git fetch` in each
checkout. It reads only what is published on each remote default branch (`origin/HEAD`) and its tags, never a
working tree, plus the manifests already published under `artifacts/`.

What it publishes is the organization's declaration (`--declaration` / COLLAB_RELEASES_DECLARATION, see
`declaration.py`); without one it reads `sources.json` as before and warns where the declaration belongs.

Per release it writes: version, date, changelog, commit (v1 fields, unchanged) and, new in v2: `class`,
`repository` and `tag_commit` (the immutable upstream reference: the authority of the release), `artifacts`
(name, sha256, bytes, tree_digest for tree tars: the verified cache of that reference) and `requires`.

- SoT components: every directory of the SoT with a `version.json` and a release table in its README
  (`| x.y.z | `commit` | text |` or `| x.y.z | tag `name` | text |`). Date: the commit date of the row's commit
  (a tag row: the tag's commit); the latest release takes the `released` of version.json when its row has none.
- Repositories (typed SREs, PHP packages, runtime and other components): every `vX.Y.Z` tag. Date: the tag's
  date. Changelog: the `## X.Y.Z` section of CHANGELOG.md on the default branch, else the tag message. A
  version with a published manifest and no tag (a repository that does not tag) is a release too.

What cannot be read is reported on stderr and skipped; the index is still written. A source whose checkout is
missing, or whose declaration is bad, keeps the entries the previous index had for it, and every version a fresh
read does not give is kept from the previous index, so a partial run never loses history. An index that cannot be
read is copied aside (`releases.json.unreadable`) before it is rebuilt. Exit status 0.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python3 -I` does not put the script's folder there

import declaration as decl  # noqa: E402
from declaration import warn  # noqa: E402

SCHEMA = "collab.releases/v2"
SOURCES_SCHEMA = "collab.release-sources/v2"
CLASSES = ("php-package", "runtime", "layer", "sot-component", "image-recipe", "service")
DEFAULT_REPOSITORY_CLASS = "layer"  # what every repository of a v1 sources file was
VERSION = r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?"
# | 0.1.2 | `abc1234` | text |      a commit
# | 0.1.2 | tag `x-v0.1.2` | text |   a tag of the SoT repository (the commit is the tag's)
# | 0.1.2 |  | text |               no commit
ROW = re.compile(
    rf"^\|\s*`?v?({VERSION})`?\s*\|\s*(?:(?i:tag)\s+`?([^`\s|]+)`?|`?([0-9a-f]{{7,40}})?`?)\s*\|\s*(.*?)\s*\|\s*$")
TAG = re.compile(rf"^v?({VERSION})$")
NUMERIC = re.compile(r"^(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")
_WARNED: set[str] = set()


def git(repo: Path, *args: str) -> str | None:
    """stdout of a git command as text (bytes that are not UTF-8 are replaced, never an error), or None."""
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else None


def warn_once(text: str) -> None:
    if text not in _WARNED:
        _WARNED.add(text)
        warn(text)


def schema_reads_as(found, name: str, versions: tuple[str, ...], where: str) -> None:
    """Warn (once) when `found` is not the neutral `collab.<name>/<v>` id. The former spelling of the same schema,
    `<owner>.<name>/<v>`, is read as it is; anything else is read as the current version, with a warning."""
    if isinstance(found, str) and found in {f"collab.{name}/{v}" for v in versions}:
        return
    if isinstance(found, str) and any(found.endswith(f".{name}/{v}") for v in versions):
        warn_once(f"{where}: schema {found!r} is the former spelling of collab.{name}; read as it is")
        return
    warn_once(f"{where}: unknown schema {found!r}; read as collab.{name}/{versions[-1]}")


def key(version) -> tuple:
    """Sort key of a version label: numeric versions by their numbers (a pre-release before its release); any other
    label (`nightly`) warns once and sorts before every numeric one, by its text."""
    text = version if isinstance(version, str) else repr(version)
    m = NUMERIC.match(text)
    if m:
        return (1, tuple(int(p) for p in m.group(1).split(".")), (1,) if m.group(2) is None else (0, m.group(2)))
    warn_once(f"version label {text!r} is not numeric; it sorts before the numeric ones")
    return (0, (), (0, text))


def safe_segment(value, limit: int = 128) -> bool:
    """A label that can name one folder: non-empty, bounded, no separator, not `.`/`..`, not hidden."""
    return (isinstance(value, str) and 0 < len(value) <= limit and not value.startswith(".")
            and not any(c in value for c in "/\\\0") and value.strip() == value)


def safe_repository(value) -> bool:
    """`owner/name`: two safe segments (each names a folder: the checkout is `<root>/<name>`)."""
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, name = value.split("/")
    return safe_segment(owner) and safe_segment(name)


def safe_component(value) -> bool:
    """A component key (`owner/repo` or a SoT path): relative, no `.`/`..` part, bounded."""
    if not isinstance(value, str) or not 0 < len(value) <= 256 or "\\" in value or "\0" in value:
        return False
    return all(part and part not in (".", "..") for part in value.split("/"))


def slug(component: str) -> str:
    """The folder name of a component under `artifacts/<class>/`: its key with `/` as `__`."""
    return component.replace("/", "__")


def release_dir(artifacts: Path, cls: str, component: str, version: str) -> Path:
    return artifacts / cls / slug(component) / version


def sot_class(component: str) -> str:
    """Recipes of images live under `containers/` in the SoT; everything else there is a SoT component."""
    return "image-recipe" if component.startswith("containers/") else "sot-component"


def _read_sources_file(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        warn(f"{path}: not found; nothing is declared to publish")
        return None
    except (OSError, ValueError) as error:
        warn(f"{path}: cannot be read ({type(error).__name__}); nothing is declared to publish")
        return None
    if not isinstance(raw, dict):
        warn(f"{path}: not a mapping; nothing is declared to publish")
        return None
    schema_reads_as(raw.get("schema"), "release-sources", ("v1", "v2"), str(path))
    return raw


def load_sources(path: Path, declaration: str | Path | None = None) -> dict:
    """What the publisher publishes: `{sot, repositories, classes, vendors, exclude, invalid, readable, origin}`.

    The organization's declaration (`--declaration` / COLLAB_RELEASES_DECLARATION) is read when given; without it
    (or when it cannot be read, or declares no `publication`) `sources.json` is read as before, with a warning that
    says where the declaration belongs. A bad entry warns and is skipped (`invalid`); the rest is read.
    `readable` is False when nothing could be read at all: callers then keep everything they had.
    """
    declared_path = decl.path_from(declaration)
    data = decl.read(declared_path) if declared_path else None
    chosen, origin = None, "none"
    if data is not None:
        chosen = decl.publication(data, declared_path)
        origin = "declaration" if chosen is not None else origin
        if chosen is None:
            warn(f"{declared_path}: declares no `publication`; reading {path} as before")
    if chosen is None:
        warn(f"the publisher's choices are read from {path}; they belong in {decl.WHERE}")
        chosen = _read_sources_file(path)
        origin = "sources" if chosen is not None else "none"
    where = str(declared_path) if origin == "declaration" else str(path)
    sources = {"sot": None, "repositories": [], "classes": {}, "invalid": set(), "readable": chosen is not None,
               "origin": origin, "vendors": decl.vendors(data, declared_path),
               "exclude": decl.string_list(f"{where}: archive_exclude", (chosen or {}).get("archive_exclude")) or []}
    chosen = chosen or {}
    sot = chosen.get("sot")
    if sot is not None and not safe_repository(sot):
        warn(f"{where}: sot {sot!r} is not `owner/name`; the SoT is skipped and its previous entries kept")
        sources["invalid"].add("<sot>")
    elif sot is not None:
        sources["sot"] = sot
    repositories = chosen.get("repositories", [])
    if not isinstance(repositories, list):
        warn(f"{where}: `repositories` is not a list; none is read and their previous entries are kept")
        sources["invalid"].add("<repositories>")
        repositories = []
    for repository in repositories:
        if not safe_repository(repository):
            warn(f"{where}: repository {repository!r} is not `owner/name`; skipped")
            if isinstance(repository, str):
                sources["invalid"].add(repository)
        elif repository not in sources["repositories"]:
            sources["repositories"].append(repository)
    classes = chosen.get("classes", {})
    if not isinstance(classes, dict):
        warn(f"{where}: `classes` is not a mapping; ignored (every repository reads as {DEFAULT_REPOSITORY_CLASS})")
        classes = {}
    for component, cls in classes.items():
        if not isinstance(component, str):
            warn(f"{where}: class key {component!r} is not a component key; ignored")
        elif cls not in CLASSES:
            warn(f"{where}: {component}: unknown class {cls!r}; the source is skipped and its previous entry kept")
            sources["invalid"].add(component)
        else:
            sources["classes"][component] = cls
    sources["repositories"] = [r for r in sources["repositories"] if r not in sources["invalid"]]
    return sources


def repository_class(sources: dict, repository: str) -> str:
    return sources["classes"].get(repository, DEFAULT_REPOSITORY_CLASS)


def component_class(sources: dict, component: str) -> str:
    """The class of a SoT component: the declared one, else by its place in the SoT."""
    return sources["classes"].get(component) or sot_class(component)


def checkout(root: Path, repository: str) -> Path | None:
    if not safe_repository(repository):
        warn(f"{repository!r}: not `owner/name`; no checkout read")
        return None
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


def sot_components(repository: str, path: Path, only: str | None = None, sources: dict | None = None) -> dict:
    out = {}
    files = (git(path, "ls-tree", "-r", "--name-only", "origin/HEAD") or "").splitlines()
    for vfile in sorted(f for f in files if f.endswith("/version.json")):
        component = vfile[: -len("/version.json")]
        if only is not None and component != only:
            continue
        if sources is not None and component in sources["invalid"]:
            continue  # its declaration is bad: warned when read, its previous entry is kept
        try:
            meta = json.loads(git(path, "show", f"origin/HEAD:{vfile}") or "")
        except ValueError:
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
        cls = component_class(sources, component) if sources is not None else sot_class(component)
        for release in releases.values():
            release.pop("tag")
            release["class"] = cls
            release["repository"] = repository
        ordered = sorted(releases.values(), key=lambda r: key(r["version"]), reverse=True)
        out[component] = {"repository": repository, "kind": "sot", "class": cls, "latest": ordered[0]["version"],
                          "releases": ordered}
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


def _text(value) -> str | None:
    return value if isinstance(value, str) else None


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
                                 "class": cls, "repository": repository, "tag_commit": commit}
    # A published release the repository has no tag for (it does not tag): the manifest is its row.
    if artifacts is not None:
        base = artifacts / cls / slug(repository)
        for manifest_file in sorted(base.glob("*/MANIFEST.json")) if base.is_dir() else []:
            manifest = read_manifest(manifest_file)
            version = manifest.get("version") if manifest else None
            if manifest and not isinstance(version, str):
                warn(f"{manifest_file}: no version label; skipped")
            if isinstance(version, str) and version not in releases:
                releases[version] = {"version": version, "date": _text(manifest.get("date")), "changelog": "",
                                     "commit": _text(manifest.get("commit")), "class": cls, "repository": repository,
                                     "tag_commit": _text(manifest.get("tag_commit"))}
    if not releases:
        warn(f"{repository}: no vX.Y.Z tag and no published manifest")
        return None
    ordered = sorted(releases.values(), key=lambda r: key(r["version"]), reverse=True)
    return {"repository": repository, "kind": "repository", "class": cls, "latest": ordered[0]["version"],
            "releases": ordered}


def read_manifest(path: Path) -> dict | None:
    """A release folder's MANIFEST.json, or None: absent says nothing; unreadable or not a mapping warns."""
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        warn(f"{path}: cannot be read ({type(error).__name__}); ignored")
        return None
    if not isinstance(manifest, dict):
        warn(f"{path}: not a mapping; ignored")
        return None
    schema_reads_as(manifest.get("schema"), "release-manifest", ("v1",), str(path))
    return manifest


def manifest_artifacts(manifest: dict, where: str) -> list[dict] | None:
    """The usable artifact rows of a manifest (each warns when it is not one), or None when the field is unusable."""
    rows = manifest.get("artifacts")
    if not isinstance(rows, list):
        warn(f"{where}: `artifacts` is not a list; ignored")
        return None
    out = []
    for artifact in rows:
        if isinstance(artifact, dict) and safe_segment(artifact.get("name"), 255):
            out.append(artifact)
        else:
            warn(f"{where}: an artifact row without a usable name; ignored")
    return out


def attach_manifests(components: dict, artifacts: Path, previous: dict | None = None) -> None:
    """Fill `artifacts` and `requires` (and `package`) of every release from its MANIFEST.json. A field the manifest
    cannot give keeps what the release had (or what the previous index had for that version), else empty."""
    previous = previous or {}
    for component_key, entry in components.items():
        before = {}
        prior = previous.get(component_key)
        if isinstance(prior, dict) and isinstance(prior.get("releases"), list):
            before = {r["version"]: r for r in prior["releases"] if isinstance(r, dict) and isinstance(r.get("version"), str)}
        for release in entry["releases"]:
            held = {**before.get(release["version"], {}), **release}
            manifest = None
            if safe_segment(release["version"]) and release.get("class") in CLASSES:
                manifest_path = release_dir(artifacts, release["class"], component_key, release["version"]) / "MANIFEST.json"
                manifest = read_manifest(manifest_path)
            else:
                warn(f"{component_key} {release['version']!r}: cannot name a release folder; its manifest is not read")
            listed = manifest_artifacts(manifest, f"{component_key} {release['version']}") if manifest else None
            if listed is not None:
                release["artifacts"] = [{k: a[k] for k in ("name", "sha256", "bytes", "tree_digest") if k in a} for a in listed]
            else:
                release["artifacts"] = held["artifacts"] if isinstance(held.get("artifacts"), list) else []
            requires = manifest.get("requires") if manifest else None
            if isinstance(requires, list):
                release["requires"] = [r for r in requires if isinstance(r, dict)]
            else:
                if manifest and "requires" in manifest:
                    warn(f"{component_key} {release['version']}: `requires` is not a list; ignored")
                release["requires"] = held["requires"] if isinstance(held.get("requires"), list) else []
            if manifest and isinstance(manifest.get("package"), str):
                release["package"] = manifest["package"]
            elif isinstance(held.get("package"), str):
                release["package"] = held["package"]


def usable_entry(component_key, entry) -> bool:
    """An index entry this script can read and rewrite: a mapping whose releases are mappings with a version."""
    return (isinstance(entry, dict) and isinstance(entry.get("releases"), list)
            and all(isinstance(r, dict) and isinstance(r.get("version"), str) for r in entry["releases"]))


def upgrade_entry(component_key: str, entry, artifacts: Path):
    """A previous (v1 or v2) entry read as v2: the fields v2 adds are filled when missing, nothing else changes. An
    entry that cannot be read that way warns and is kept exactly as it was."""
    if not usable_entry(component_key, entry):
        warn(f"{component_key}: the previous index entry is malformed; kept as it was")
        return entry
    cls = entry.get("class") if entry.get("class") in CLASSES else (
        sot_class(component_key) if entry.get("kind") == "sot" else DEFAULT_REPOSITORY_CLASS)
    entry = {**entry, "class": cls}
    repository = entry.get("repository") if isinstance(entry.get("repository"), str) else None
    entry["releases"] = [{**r, "class": r.get("class") if r.get("class") in CLASSES else cls,
                          "repository": r.get("repository") or repository, "tag_commit": r.get("tag_commit")}
                         for r in entry["releases"]]
    attach_manifests({component_key: entry}, artifacts)
    return entry


def merge_history(component_key: str, fresh: dict, prior) -> dict:
    """`fresh` with every release of `prior` it does not have (a checkout with only some tags, a manifest that cannot
    be read): history is never dropped by a partial read. Each kept version warns."""
    if not usable_entry(component_key, prior):
        return fresh
    have = {r["version"] for r in fresh["releases"]}
    kept = [r for r in prior["releases"] if r["version"] not in have]
    if not kept:
        return fresh
    warn(f"{component_key}: {', '.join(r['version'] for r in kept)} not read this time; kept from the previous index")
    ordered = sorted(fresh["releases"] + kept, key=lambda r: key(r["version"]), reverse=True)
    return {**fresh, "releases": ordered, "latest": ordered[0]["version"]}


def read_component(root: Path, sources: dict, component: str, artifacts: Path, previous: dict | None = None) -> dict | None:
    """The fresh index entry of one component (a repository `owner/repo` or a SoT path), or None."""
    if component in sources["repositories"]:
        path = checkout(root, component)
        entry = repository_component(component, path, repository_class(sources, component), artifacts)
    elif sources["sot"] and component not in sources["invalid"]:
        path = checkout(root, sources["sot"])
        entry = sot_components(sources["sot"], path, only=component, sources=sources).get(component) if path is not None else None
    else:
        return None
    if entry is not None:
        attach_manifests({component: entry}, artifacts, previous)
    return entry


def build(root: Path, sources: dict, artifacts: Path, previous: dict | None = None) -> dict:
    """The `components` of the index. `previous` is the old `components`: entries of a source that cannot be read
    (no checkout, a bad declaration, no declaration at all) are kept from it, and so is every version a fresh read
    does not give."""
    previous = previous or {}
    fresh, kept = {}, {}
    if not sources.get("readable", True):
        warn("nothing could be read about what to publish; every previous entry is kept")
        kept.update(previous)
    sot = checkout(root, sources["sot"]) if sources["sot"] else None
    if sot is not None:
        fresh.update(sot_components(sources["sot"], sot, sources=sources))
    for k, entry in previous.items():
        if isinstance(entry, dict) and entry.get("kind") == "sot" and k not in fresh:
            if sot is None or k in sources["invalid"]:
                kept[k] = entry
    for repository in sources["repositories"]:
        path = checkout(root, repository)
        entry = repository_component(repository, path, repository_class(sources, repository), artifacts)
        if entry is not None:
            fresh[repository] = entry
        elif path is None and repository in previous:
            kept[repository] = previous[repository]
    for k in sources["invalid"]:
        if k in previous and k not in fresh:
            kept[k] = previous[k]
    if "<repositories>" in sources["invalid"]:
        for k, entry in previous.items():
            if isinstance(entry, dict) and entry.get("kind") == "repository" and k not in fresh:
                kept[k] = entry
    for k in sorted(kept):
        warn(f"{k}: kept from the previous index")
    attach_manifests(fresh, artifacts, previous)
    fresh = {k: merge_history(k, e, previous.get(k)) for k, e in fresh.items()}
    components = {**{k: upgrade_entry(k, e, artifacts) for k, e in kept.items() if k not in fresh}, **fresh}
    return dict(sorted(components.items()))


def load_index(path: Path) -> tuple[dict, str]:
    """`(components, state)`: state `ok`, `missing` (no file: an empty index) or `unreadable` (the file is there and
    is not an index: nothing of it can be merged; it warns and the caller preserves it before writing)."""
    if not path.exists():
        return {}, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        warn(f"{path}: the index cannot be read ({type(error).__name__})")
        return {}, "unreadable"
    comps = data.get("components") if isinstance(data, dict) else None
    if not isinstance(comps, dict):
        warn(f"{path}: the index has no `components` mapping")
        return {}, "unreadable"
    schema_reads_as(data.get("schema"), "releases", ("v1", "v2"), str(path))
    return comps, "ok"


def read_index(path: Path) -> dict:
    return load_index(path)[0]


def preserve(path: Path) -> Path:
    """Copy an unreadable file aside (`<name>.unreadable[-N]`, never over an earlier copy) before it is rewritten."""
    target, n = path.with_name(path.name + ".unreadable"), 1
    while target.exists():
        target, n = path.with_name(f"{path.name}.unreadable-{n}"), n + 1
    shutil.copy2(path, target)
    warn(f"{path}: its unreadable bytes are kept at {target}")
    return target


def write_index(path: Path, components: dict) -> None:
    """Write the index through a staged file that replaces the live one only once it is complete."""
    index = {"schema": SCHEMA,
             "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
             "components": dict(sorted(components.items()))}
    part = path.with_name(f".{path.name}.part")
    part.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(part, path)


def count_releases(components: dict) -> int:
    return sum(len(c["releases"]) for c in components.values() if usable_entry(None, c))


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    parser = decl.Parser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=here.parent, help="directory holding the checkouts (default: next to this repo)")
    parser.add_argument("--declaration", help=f"the organization's declaration (default: ${decl.ENV}); without it, --sources is read")
    parser.add_argument("--sources", type=Path, default=here / "sources.json", help="the former list of what to publish")
    parser.add_argument("--artifacts", type=Path, default=here / "artifacts")
    parser.add_argument("--out", type=Path, default=here / "releases.json")
    try:
        args, unknown = parser.parse_known_args(argv)
    except decl.UsageWarning as problem:
        warn(f"{problem}; nothing done (see --help)")
        return 0
    for extra in unknown:
        warn(f"unknown argument {extra!r}; ignored")
    previous, state = load_index(args.out)
    if state == "unreadable":
        preserve(args.out)
    components = build(args.root, load_sources(args.sources, args.declaration), args.artifacts, previous)
    write_index(args.out, components)
    print(f"{args.out}: {len(components)} components, {count_releases(components)} releases, "
          f"{len(decl.WARNINGS)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
