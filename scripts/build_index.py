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

- Sub-components `<owner>/<name>/<path>` of a declared repository (declared in `classes`, or already published):
  one release per published manifest.

A checkout is `<root>/<owner>/<name>` or `<root>/<name>`, and only one whose `origin` remote names that repository
is read (another repository with the same name is never labelled as this one).

What cannot be read is reported on stderr and skipped; the index is still written. A source whose checkout is
missing, whose declaration is bad, or that cannot be read this time (a version.json that is not JSON, no tag
read), keeps the entries the previous index had for it, and every version a fresh read does not give is kept from
the previous index, so a partial run never loses history. A row whose commit this read cannot resolve keeps the
one resolved before. A published version keeps the reference its manifest is bound to: when the source now names
another commit (a moved tag) it warns and the row keeps the published commit with its artifacts. An index that
cannot be read is copied aside (`releases.json.unreadable`) before it is rebuilt. Exit status 0.
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


def load_sources(path: Path, declaration: str | Path | None = None, cache: Path | None = None) -> dict:
    """What the publisher publishes: `{sot, repositories, classes, vendors, exclude, exclude_known, invalid,
    readable, origin}`.

    The organization's declaration (`--declaration` / COLLAB_RELEASES_DECLARATION) is read when given; when it
    cannot be read (or its `publication` is malformed) the last verified copy in `cache` is used, with a warning.
    Without a declaration (or with no verified copy, or when it declares no `publication`) `sources.json` is read
    as before, with a warning that says where the declaration belongs. A bad entry warns and is skipped
    (`invalid`); the rest is read. `readable` is False when nothing could be read at all: callers then keep
    everything they had. `exclude_known` is False when a declaration was given and which paths it excludes from
    archives cannot be known (it could not be read, its `archive_exclude` is malformed, and there is no verified
    copy): no archive is built then, so a path the organization excludes is never packed.
    """
    declared_path = decl.path_from(declaration)
    data, digest = decl.read_with_digest(declared_path) if declared_path else (None, None)
    origin, exclude_known = "none", True
    if declared_path is not None:
        well_formed = data is not None and decl.exclusions(data.get("publication")) is not None
        if well_formed:
            decl.remember(cache, declared_path, digest, data)
        else:
            cached = decl.recall(cache)
            if cached is not None and decl.exclusions(cached["data"].get("publication")) is not None:
                warn(f"{declared_path}: using the last verified copy of the declaration (sha256 "
                     f"{str(cached.get('sha256'))[:12]}, read {cached.get('read_at')}, {decl.age(cached.get('read_at'))})")
                data, origin = cached["data"], "cache"
            else:
                if data is not None:
                    warn(f"{declared_path}: `publication` or its `archive_exclude` is malformed")
                warn(f"{declared_path}: no verified copy of the declaration is kept here; which paths it excludes "
                     "from archives is not known, so no archive is built (the index is still updated)")
                exclude_known = False
    chosen = None
    if data is not None:
        chosen = decl.publication(data, declared_path)
        origin = (origin if origin == "cache" else "declaration") if chosen is not None else origin
        if chosen is None and data.get("publication") is None:
            warn(f"{declared_path}: declares no `publication`; reading {path} as before")
    if chosen is None:
        warn(f"the publisher's choices are read from {path}; they belong in {decl.WHERE}")
        chosen = _read_sources_file(path)
        origin = "sources" if chosen is not None else "none"
    where = str(declared_path) if origin in ("declaration", "cache") else str(path)
    excluded = decl.exclusions(chosen) if chosen is not None else []
    sources = {"sot": None, "repositories": [], "classes": {}, "invalid": set(), "readable": chosen is not None,
               "origin": origin, "vendors": decl.vendors(data, declared_path),
               "exclude": excluded or [], "exclude_known": exclude_known and excluded is not None}
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


def origin_identity(path: Path) -> str | None:
    """The `owner/name` the checkout's `origin` remote names (the last two segments of its URL), or None. The URL
    itself is never printed or returned: it may carry credentials."""
    url = (git(path, "config", "--get", "remote.origin.url") or "").strip()
    if not url:
        return None
    if "://" in url:
        rest = url.split("://", 1)[1]
        rest = rest.split("/", 1)[1] if "/" in rest else ""
    elif re.match(r"^[^/\\]+:", url):  # scp-like `host:owner/name`
        rest = url.split(":", 1)[1]
    else:  # a local path
        rest = url
    rest = rest.split("?", 1)[0].split("#", 1)[0].replace("\\", "/")
    parts = [p for p in rest.split("/") if p]
    if len(parts) < 2:
        return None
    name = parts[-1][:-4] if parts[-1].endswith(".git") else parts[-1]
    return f"{parts[-2]}/{name}" if safe_segment(parts[-2]) and safe_segment(name) else None


def checkout(root: Path, repository: str) -> Path | None:
    """The checkout of `owner/name`: `<root>/<owner>/<name>`, else `<root>/<name>`, and only one whose own top
    level holds origin/HEAD and whose `origin` remote names this repository. A checkout of another repository with
    the same name, or one whose origin names no repository, is not read (it warns): its tags would be labelled as
    another repository's."""
    if not safe_repository(repository):
        warn(f"{repository!r}: not `owner/name`; no checkout read")
        return None
    owner, name = repository.split("/")
    seen = []
    for path in (root / owner / name, root / name):
        if not path.is_dir() or git(path, "rev-parse", "--verify", "-q", "origin/HEAD") is None:
            continue
        top = (git(path, "rev-parse", "--show-toplevel") or "").strip()
        if not top or Path(top).resolve() != path.resolve():
            continue  # a folder inside another checkout, not a checkout of its own
        seen.append(path)
        found = origin_identity(path)
        if found is None:
            warn(f"{repository}: the checkout at {path} has no `origin` remote that names a repository; it is not "
                 f"read (it cannot be shown to be {repository})")
        elif found.lower() != repository.lower():
            warn(f"{repository}: the checkout at {path} is of {found}, not of {repository}; it is not read")
        else:
            return path
    if not seen:
        warn(f"{repository}: no checkout with origin/HEAD at {root / owner / name} or {root / name}")
    return None


def same_commit(one, two) -> bool:
    """Two commit references name the same commit: equal, or one an abbreviation (7+ hex) of the other."""
    if not (isinstance(one, str) and isinstance(two, str)) or min(len(one), len(two)) < 7:
        return False
    one, two = one.lower(), two.lower()
    return one.startswith(two) or two.startswith(one)


def parent_repository(sources: dict, component: str) -> str | None:
    """The declared repository a sub-component key `<owner>/<name>/<path>` belongs to, or None."""
    parts = component.split("/") if isinstance(component, str) else []
    if len(parts) > 2 and "/".join(parts[:2]) in sources["repositories"]:
        return "/".join(parts[:2])
    return None


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


def sot_components(repository: str, path: Path, only: str | None = None, sources: dict | None = None,
                   failed: set | None = None) -> dict:
    """The SoT's components. A component that cannot be read (its version.json is not JSON, or has no version
    label) is added to `failed`, and so is `*` when the tree itself cannot be listed: callers keep their previous
    entries, because a read that failed is never a reason to drop history."""
    failed = failed if failed is not None else set()
    out = {}
    listing = git(path, "ls-tree", "-r", "--name-only", "origin/HEAD")
    if listing is None:
        warn(f"{repository}: the tree of origin/HEAD cannot be listed; its components are not read")
        failed.add("*")
        return out
    files = listing.splitlines()
    for vfile in sorted(f for f in files if f.endswith("/version.json")):
        component = vfile[: -len("/version.json")]
        if only is not None and component != only:
            continue
        if sources is not None and component in sources["invalid"]:
            continue  # its declaration is bad: warned when read, its previous entry is kept
        try:
            meta = json.loads(git(path, "show", f"origin/HEAD:{vfile}") or "")
        except ValueError:
            warn(f"{component}: version.json is not JSON; its previous entry is kept")
            failed.add(component)
            continue
        if isinstance(meta, dict) and meta.get("component") not in (None, component):
            continue  # a schema version file, not a component's
        if not isinstance(meta, dict) or not isinstance(meta.get("version"), str):
            warn(f"{component}: version.json has no version label; its previous entry is kept")
            failed.add(component)
            continue
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
    # A published release the repository has no tag for (it does not tag): the manifest is its row. The declared
    # class is looked at first; a version published under another class (`--class`) is found there.
    if artifacts is not None:
        for row in manifest_rows(artifacts, repository, repository, cls):
            if row["version"] not in releases:
                releases[row["version"]] = row
    if not releases:
        warn(f"{repository}: no vX.Y.Z tag and no published manifest")
        return None
    ordered = sorted(releases.values(), key=lambda r: key(r["version"]), reverse=True)
    return {"repository": repository, "kind": "repository", "class": cls, "latest": ordered[0]["version"],
            "releases": ordered}


def manifest_rows(artifacts: Path, component: str, repository: str, cls: str) -> list[dict]:
    """A release row for every published MANIFEST.json of `component`, in its declared class first and then in any
    other class. A manifest that names another component or another version than its folder is not one of them."""
    rows, seen = [], set()
    for folder_class in (cls, *[c for c in CLASSES if c != cls]):
        base = artifacts / folder_class / slug(component)
        for manifest_file in sorted(base.glob("*/MANIFEST.json")) if base.is_dir() else []:
            manifest = read_manifest(manifest_file)
            if not manifest:
                continue
            version = manifest.get("version")
            if not isinstance(version, str):
                warn(f"{manifest_file}: no version label; skipped")
                continue
            if not manifest_belongs(manifest, component, manifest_file.parent.name, str(manifest_file)) or version in seen:
                continue
            seen.add(version)
            rows.append({"version": version, "date": _text(manifest.get("date")), "changelog": "",
                         "commit": _text(manifest.get("commit")), "class": folder_class, "repository": repository,
                         "tag_commit": _text(manifest.get("tag_commit"))})
    return rows


def manifest_belongs(manifest: dict, component: str, version: str, where: str) -> bool:
    """A manifest is the one of `component` `version` unless it names another one (it then warns and is not used:
    its artifacts would be advertised under a release they do not belong to)."""
    named, label = manifest.get("component"), manifest.get("version")
    if isinstance(named, str) and named != component:
        warn(f"{where}: the manifest is of {named}, not of {component}; not used")
        return False
    if isinstance(label, str) and label != version:
        warn(f"{where}: the manifest is of version {label}, not of {version}; not used")
        return False
    return True


def find_manifest(artifacts: Path, cls: str, component: str, version: str) -> tuple[dict | None, str]:
    """`(manifest, class)` of one release: in its own class's folder, else (a `--class` override) in the one other
    class that has it. `(None, cls)` when no manifest is published."""
    own = release_dir(artifacts, cls, component, version) / "MANIFEST.json"
    if own.exists():
        return read_manifest(own), cls
    for other in CLASSES:
        path = release_dir(artifacts, other, component, version) / "MANIFEST.json"
        if other != cls and path.exists():
            manifest = read_manifest(path)
            if manifest is not None:
                return manifest, other
    return None, cls


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
            prior_release = before.get(release["version"], {})
            keep_resolved_reference(component_key, release, prior_release)
            held = {**prior_release, **release}
            manifest = None
            if safe_segment(release["version"]) and release.get("class") in CLASSES:
                manifest, found_class = find_manifest(artifacts, release["class"], component_key, release["version"])
                where = f"{component_key} {release['version']}"
                if manifest is not None and not manifest_belongs(manifest, component_key, release["version"], where):
                    manifest = None
                if manifest is not None:
                    release["class"] = found_class
                    keep_published_binding(component_key, release, manifest)
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


REFERENCE_FIELDS = ("commit", "tag_commit", "date")


def keep_resolved_reference(component_key: str, release: dict, prior: dict) -> None:
    """A fresh row whose reference this read could not resolve (a checkout without the tag its README row names)
    keeps the commit, tag_commit and date the previous index resolved for the same version."""
    if release.get("commit") is not None or not isinstance(prior.get("commit"), str):
        return
    warn(f"{component_key} {release['version']}: its commit cannot be resolved by this read; the previously resolved "
         f"{prior['commit'][:12]} is kept")
    for field in REFERENCE_FIELDS:
        if release.get(field) is None and isinstance(prior.get(field), str):
            release[field] = prior[field]


def keep_published_binding(component_key: str, release: dict, manifest: dict) -> None:
    """The artifacts of a published version are bound to the reference its manifest recorded. When a fresh read of
    the source names another commit for the version (a tag moved), the row keeps the published binding (commit,
    tag_commit, date of the manifest) and warns: the cached bytes are never advertised under another commit."""
    field = "tag_commit" if isinstance(manifest.get("tag_commit"), str) and isinstance(release.get("tag_commit"), str) else "commit"
    bound, fresh = manifest.get(field), release.get(field)
    if isinstance(bound, str) and isinstance(fresh, str) and not same_commit(bound, fresh):
        warn(f"{component_key} {release['version']}: the source now names {fresh[:12]}, but the version was published "
             f"from {bound[:12]}; the published binding is kept (a published version takes no other bytes: cut a new one)")
        for name in REFERENCE_FIELDS:
            if isinstance(manifest.get(name), str):
                release[name] = manifest[name]
            elif name != "date":
                release[name] = None
    elif release.get("commit") is None:
        for name in REFERENCE_FIELDS:
            if release.get(name) is None and isinstance(manifest.get(name), str):
                release[name] = manifest[name]


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


TREE_CLASSES = ("service", "image-recipe", "sot-component")


def subcomponent_class(sources: dict, component: str, artifacts: Path | None = None) -> str | None:
    """The class of a sub-component `<owner>/<name>/<path>`: the declared one, else the class it was published as."""
    declared = sources["classes"].get(component)
    if declared:
        return declared
    for cls in CLASSES:
        base = artifacts / cls / slug(component) if artifacts is not None else None
        if base is not None and base.is_dir() and any(base.glob("*/MANIFEST.json")):
            return cls
    return None


def subcomponents(sources: dict, artifacts: Path) -> list[str]:
    """Sub-component keys of the declared repositories: the ones `classes` declares (`<owner>/<name>/<path>`) and
    the ones already published (a manifest whose `component` is such a key)."""
    found = {k for k in sources["classes"] if parent_repository(sources, k)}
    for repository in sources["repositories"]:
        prefix = slug(repository) + "__"
        for class_dir in (d for d in sorted(artifacts.iterdir()) if d.is_dir()) if artifacts.is_dir() else []:
            for folder in sorted(class_dir.glob(prefix + "*")):
                for manifest_file in sorted(folder.glob("*/MANIFEST.json")):
                    manifest = read_manifest(manifest_file) or {}
                    named = manifest.get("component")
                    if isinstance(named, str) and slug(named) == folder.name and parent_repository(sources, named) == repository:
                        found.add(named)
                        break
    return sorted(found)


def subcomponent_entry(component: str, repository: str, path: Path | None, cls: str | None, artifacts: Path) -> dict | None:
    """The entry of a sub-component: one release per published manifest (it is published from its repository's tag
    or a `--commit`), with the changelog section of its repository when the checkout is here."""
    rows = manifest_rows(artifacts, component, repository, cls or "service")
    if not rows:
        return None
    sections = changelog_sections(git(path, "show", "origin/HEAD:CHANGELOG.md") or "") if path is not None else {}
    for row in rows:
        row["changelog"] = sections.get(row["version"], "")
    ordered = sorted(rows, key=lambda r: key(r["version"]), reverse=True)
    return {"repository": repository, "kind": "repository", "class": cls or ordered[0]["class"],
            "latest": ordered[0]["version"], "releases": ordered}


def read_component(root: Path, sources: dict, component: str, artifacts: Path, previous: dict | None = None) -> dict | None:
    """The fresh index entry of one component (a repository `owner/repo`, one of its sub-components
    `owner/repo/<path>`, or a SoT path), or None."""
    parent = parent_repository(sources, component)
    if parent is not None:
        path = checkout(root, parent)
        entry = subcomponent_entry(component, parent, path, subcomponent_class(sources, component, artifacts), artifacts)
    elif component in sources["repositories"]:
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
    failed: set = set()
    if sot is not None:
        fresh.update(sot_components(sources["sot"], sot, sources=sources, failed=failed))
    for k, entry in previous.items():
        if isinstance(entry, dict) and entry.get("kind") == "sot" and k not in fresh:
            if sot is None or k in sources["invalid"] or k in failed or "*" in failed:
                kept[k] = entry
    paths = {}
    for repository in sources["repositories"]:
        path = paths[repository] = checkout(root, repository)
        entry = repository_component(repository, path, repository_class(sources, repository), artifacts)
        if entry is not None:
            fresh[repository] = entry
        elif repository in previous:
            kept[repository] = previous[repository]  # nothing read this time is never a reason to drop history
    for component in subcomponents(sources, artifacts):
        parent = parent_repository(sources, component)
        entry = subcomponent_entry(component, parent, paths.get(parent), subcomponent_class(sources, component, artifacts), artifacts)
        if entry is not None:
            fresh[component] = entry
    for k, entry in previous.items():
        if k not in fresh and parent_repository(sources, k):
            kept[k] = entry  # a sub-component whose manifests cannot be read this time
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
    sources = load_sources(args.sources, args.declaration, decl.cache_file(args.out.parent))
    components = build(args.root, sources, args.artifacts, previous)
    write_index(args.out, components)
    print(f"{args.out}: {len(components)} components, {count_releases(components)} releases, "
          f"{len(decl.WARNINGS)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
