#!/usr/bin/env python3
"""Publish one release of one component: build its artifacts from the tag, write MANIFEST.json, update the index.

    python3 -I scripts/publish.py <component> <version> [--declaration FILE] [--root DIR] [--files DIR] ...

`<component>` is the key the index uses: `owner/repo` of a repository the publisher declares, or the path of a
SoT component (`skills/example-skill`, `containers/example-recipe`). `<version>` is the release label (a leading
`v` is dropped). It is the last row of each release procedure: run it after the tag (or, for the SoT, after the
version PR is merged), with every checkout fetched in `--root`.

What the publisher publishes (repositories, classes, Composer vendor filter, archive exclusions) is the
organization's declaration, given with `--declaration` or COLLAB_RELEASES_DECLARATION (see `declaration.py`).
Without it `sources.json` is read as before, with a warning that says where the declaration belongs.

What it builds, by class (`--class` overrides the declared one):

- php-package: a stored zip of `git archive` of the tag `vX.Y.Z`, which must hold `composer.json`.
- runtime, layer: the released files of the version, copied from the directory given with `--files` (wheels,
  `*.component.json`, `*SHA256SUMS`, `install.py`, lock, manifest, notes). The sums files are checked, and the
  files are bound to the release by its own asset list (a `collab.component-release/v1` `*.component.json` or a
  `collab.release/v*` `release-manifest.json` that names this repository, this version and, when the release has
  a tag, the tag's commit; a list that omits one of them binds nothing): only the files it lists, with the sha256
  it records, are published. Without such a list nothing from `--files` is published under
  the version (it warns; the index is still updated). A directory whose every asset list names another release
  is refused.
- sot-component, image-recipe: the tree tar of the component's directory at the commit of its row in the
  README release table (`--commit` overrides a row without a tag; with a tag it can only name the tag's commit,
  another is a refusal), with its `tree_digest` (from the installed cli).
- service, and an image-recipe of a repository other than the SoT: the tree tar of `--path` (default: the whole
  tree) at the tag's commit or `--commit`.
- a sub-component `<owner>/<name>/<path>` of a declared repository (declared in `publication.classes`, or
  published with `--class` other than the repository's and `--path`): the tree tar of `<path>`, under its own
  index key, so it and its repository's own releases are both found.

A checkout is `<root>/<owner>/<name>` or `<root>/<name>`, and only one whose `origin` remote names that
repository is read.

The authority of a release is its upstream reference, `repository` + `tag_commit` (or the row's `commit`),
recorded in the manifest and in the index; the bytes stored here are a verified cache of it. Archives never carry
the paths the declaration lists in `publication.archive_exclude` (the organization's own declarations).

Only what is published is read: tags, `origin/HEAD` and the objects of a checkout, never a working tree. What
cannot be read (an undeclared component, no checkout, no tag, no `composer.json`, no `--files`, an unreadable
file, a malformed sums line, a commit that is not on the default branch) warns on stderr and is skipped; the
index is rewritten anyway, keeping every version it had. Every byte is staged and verified, and every destination
checked, before any live file is replaced; a failure while replacing puts the replaced files back. The only
refusal is the wrong artifact: a well-formed sum that is not the file's, `--files` of another release, a
`--commit` that is not the commit of the release's tag, a version whose stored bytes differ from the rebuilt ones, a copy or a tar that does not read back as built. Then no live file
changes and the exit status is 3. A stale sha256 in a manifest whose stored bytes are these is repaired with a
warning. Everything else exits 0.

It ends with `git add` and one commit of `releases.json`, the release's MANIFEST.json and the artifacts it lists
(nothing else in the folder), unless `--no-commit`, or when an artifact that must go through Git LFS would not be
stored as the LFS pointer of its declared bytes (no `git lfs`, `.gitattributes` does not cover it, another filter).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python3 -I` does not put the script's folder there

import artifacts as art  # noqa: E402
import build_index as idx  # noqa: E402
import declaration as decl  # noqa: E402
from build_index import warn  # noqa: E402

MANIFEST_SCHEMA = "collab.release-manifest/v1"
RESERVED = ("MANIFEST.json",)
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Unverified(Exception):
    """Staged bytes that could not be read back: nothing replaces a live file; the release stays as it was."""


def is_ancestor(path: Path, commit: str) -> bool:
    return idx.git(path, "merge-base", "--is-ancestor", commit, "origin/HEAD") is not None


def read_json_at(path: Path, commit: str, name: str) -> dict | None:
    try:
        data = json.loads(idx.git(path, "show", f"{commit}:{name}") or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def composer_info(path: Path, tag: str, vendors: list[str] | None) -> tuple[str | None, list[dict]]:
    """The package name and the Composer requirements a release records: those of the declared vendors
    (`packages.vendors`), or every one of them, with a warning, when no vendor filter is declared."""
    data = read_json_at(path, f"refs/tags/{tag}", "composer.json") or {}
    name = data.get("name") if isinstance(data.get("name"), str) else None
    required = data.get("require", {})
    if not isinstance(required, dict):
        warn(f"composer.json of {tag}: `require` is not a mapping; no requirement recorded")
        required = {}
    rows = [{"kind": "composer", "name": n, "constraint": str(c)} for n, c in sorted(required.items())]
    if vendors is None:
        idx.warn_once(f"no Composer vendor filter is declared (it belongs in {decl.WHERE}); "
                      "every requirement of composer.json is recorded")
        return name, rows
    return name, [r for r in rows if "/" in r["name"] and r["name"].split("/")[0].lower() in vendors]


def component_requires(path: Path, commit: str, component: str) -> list[dict]:
    """The SoT component or layer versions a `version.json` pins: each object value with `component` and `version`."""
    meta = read_json_at(path, commit, f"{component}/version.json") or {}
    return [{"kind": "component", "name": v["component"], "constraint": v["version"]}
            for _, v in sorted(meta.items())
            if isinstance(v, dict) and isinstance(v.get("component"), str) and isinstance(v.get("version"), str)]


def is_sums(name: str) -> bool:
    return name == "SHA256SUMS" or name.endswith(".SHA256SUMS")


def sums_entries(text: str, name: str, quiet: bool = False) -> list[tuple[str, str]]:
    """The well-formed `(sha256, file name)` lines of a sums file; a malformed line warns (unless `quiet`)."""
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        parts = line.strip().split(None, 1)
        if not parts or parts[0].startswith("#"):
            continue
        if len(parts) != 2:
            if not quiet:
                warn(f"{name}:{number}: not `<sha256>  <name>`; not checked")
            continue
        listed, target = parts[0].lower(), parts[1].lstrip("*").strip()
        if not HEX64.match(listed):
            if not quiet:
                warn(f"{name}:{number}: the sum for {target} is not a SHA-256; not checked")
            continue
        rows.append((listed, target))
    return rows


def sums_check(directory: Path, digests: dict[str, str]) -> None:
    """A `SHA256SUMS` file lists `<sha256>  <name>`. A well-formed sum that is not the listed file's is a Refusal
    (the bytes are not the released ones); a malformed line, an unreadable sums file or a file that is not in the
    directory warns and is not checked."""
    for name in sorted(digests):
        if not is_sums(name):
            continue
        try:
            text = (directory / name).read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            warn(f"{name}: cannot be read ({type(error).__name__}); its sums are not checked")
            continue
        for listed, target in sums_entries(text, name):
            if target not in digests:
                warn(f"{name} lists {target}, which is not in the directory")
            elif digests[target] != listed:
                raise art.Refusal(f"{name} lists another sha256 for {target} than the file has")


ASSET_LIST_SCHEMA = re.compile(r"(?:^|\.)(?:component-release|release)/v\d+$")


def asset_list(entry: dict) -> dict | None:
    """The release asset list a `--files` entry is, or None: a JSON file of the runtime's release contracts
    (`collab.component-release/v1` `*.component.json`, `collab.release/v*` `release-manifest.json`) whose record
    (`component` or `runtime`) names the repository, version and commit it was built from, and whose `files`
    list the name and sha256 of each released file."""
    if not entry["name"].endswith(".json"):
        return None
    try:
        raw = entry["src"].read_bytes() if isinstance(entry["src"], Path) else entry["src"]
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("schema"), str) or not ASSET_LIST_SCHEMA.search(data["schema"]):
        return None
    record = next((data[k] for k in ("component", "runtime") if isinstance(data.get(k), dict)), None)
    if record is None or not isinstance(data.get("files"), list):
        return None
    files = {}
    for row in data["files"]:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and isinstance(row.get("sha256"), str):
            files[row["name"]] = row["sha256"].lower()
    versions = {str(v).removeprefix("v") for v in (data.get("version"), record.get("version")) if isinstance(v, str)}
    return {"repository": record.get("repository") if isinstance(record.get("repository"), str) else None,
            "commit": record.get("commit") if isinstance(record.get("commit"), str) else None,
            "versions": versions, "files": files}


def bind_release_files(component: str, version: str, tag_commit: str | None, listing: list[dict]) -> list[dict]:
    """The `--files` entries that belong to `component` `version`, by the release's own asset list.

    An asset list binds when it names this repository AND this version AND (when the release has a tag) the commit
    of the tag. A commit alone is not a release (two tags can point at one commit), a version alone is not one
    either (it says nothing of where the files were built), and a list that omits the repository, the version or
    the tag's commit binds nothing. Every file it lists must have the sha256 it records; a sums file binds when
    every file it lists is bound. When every asset list that names a release names another one (another repository,
    commit or version), the directory is another release's: a Refusal. With no asset list that can bind, nothing is
    published under the version (it warns: the index is still updated and keeps what it had)."""
    agreeing, other = [], []
    for entry in listing:
        found = asset_list(entry)
        if found is None:
            continue
        if not found["repository"]:
            warn(f"{entry['name']}: an asset list that names no repository; it binds nothing")
        elif found["repository"].lower() != component.lower():
            other.append(f"{entry['name']} is the asset list of {found['repository']}")
        elif not found["versions"]:
            warn(f"{entry['name']}: an asset list that names no version; it binds nothing (a commit can carry "
                 "several tags, so it does not tell which release the files are)")
        elif found["versions"] != {version}:
            other.append(f"{entry['name']} lists the files of version {', '.join(sorted(found['versions']))}, "
                         f"not of {version}")
        elif tag_commit and not found["commit"]:
            warn(f"{entry['name']}: an asset list that names no commit; it binds nothing to the tag v{version} "
                 f"({tag_commit[:12]})")
        elif tag_commit and not idx.same_commit(found["commit"], tag_commit):
            other.append(f"{entry['name']} lists the files of commit {found['commit'][:12]}, not of the tag "
                         f"v{version} ({tag_commit[:12]})")
        else:
            if not tag_commit:
                warn(f"{entry['name']}: bound to {component} {version} by its repository and version only (there "
                     "is no tag commit to compare with)")
            agreeing.append((entry, found))
    if not agreeing:
        if other:
            raise art.Refusal(f"the --files of {component} {version} are another release's ({'; '.join(other)})")
        warn(f"{component} {version}: --files holds no asset list of this release (a *.component.json or a "
             "release-manifest.json naming its repository and commit); nothing from it is published under this "
             "version, and the index keeps what it had")
        return []
    for problem in other:
        warn(f"{problem}; published only if this release's asset list names it")
    by_name = {e["name"]: e for e in listing}
    declared: dict[str, str] = {}
    for entry, found in agreeing:
        for name, digest in found["files"].items():
            if declared.get(name, digest) != digest:
                raise art.Refusal(f"two asset lists of {component} {version} record other sha256 for {name}")
            declared[name] = digest
    bound = {entry["name"] for entry, _ in agreeing}
    for name, digest in sorted(declared.items()):
        if name not in by_name:
            warn(f"{name}: listed by the asset list of {component} {version}, not readable in --files; not published")
        elif by_name[name]["sha256"] != digest:
            raise art.Refusal(f"{name} has another sha256 than the asset list of {component} {version} records")
        else:
            bound.add(name)
    for entry in listing:
        if is_sums(entry["name"]) and entry["name"] not in bound:
            try:
                text = entry["src"].read_text(encoding="utf-8", errors="replace") if isinstance(entry["src"], Path) else entry["src"].decode("utf-8", "replace")
            except OSError:
                continue
            targets = {target for _, target in sums_entries(text, entry["name"], quiet=True)}
            if targets and targets <= bound:
                bound.add(entry["name"])
    for entry in listing:
        if entry["name"] not in bound:
            warn(f"{entry['name']}: not in the asset list of {component} {version}; not published")
    return [e for e in listing if e["name"] in bound]


def gather(registry_sources: dict, root: Path, component: str, version: str, cls: str | None, files_dir: Path | None,
           tree_path: str | None, commit_override: str | None) -> dict | None:
    """What can be read about this release: `{repository, class, tag, tag_commit, commit, date, path, package,
    requires, payloads: [(name, bytes | Path)], tree_at}`. None when nothing is declared for the component (it
    warns: the publication is a no-op)."""
    info = {"component": component, "version": version, "tag": None, "tag_commit": None, "commit": None,
            "date": None, "path": None, "package": None, "requires": [], "payloads": []}
    exclude = registry_sources.get("exclude") or []
    if component in registry_sources["invalid"]:
        warn(f"{component}: its declaration is not usable (warned above); nothing published")
        return None
    parent = idx.parent_repository(registry_sources, component)
    if parent is not None:
        info["repository"] = parent
        info["class"] = cls or registry_sources["classes"].get(component)
        if info["class"] not in idx.TREE_CLASSES:
            warn(f"{component}: a sub-component of {parent} is a tree ({', '.join(idx.TREE_CLASSES)}); its class "
                 f"is {info['class'] or 'not declared'} (declare it in publication.classes, or give --class); "
                 "nothing published")
            return None
        path = idx.checkout(root, parent)
        tag = f"v{version}"
        commit = idx.tag_commit(path, tag) if path is not None else None
        info.update(tag=tag if commit else None, tag_commit=commit, commit=commit)
        if commit:
            info["date"] = (idx.git(path, "for-each-ref", "--format=%(creatordate:short)", f"refs/tags/{tag}") or "").strip() or None
        subtree = component[len(parent) + 1:]
        if tree_path and tree_path.strip("/") != subtree:
            warn(f"--path {tree_path!r}: {component} is the tree at {subtree}; --path is ignored")
        tree_at(info, path, commit_override or commit, subtree, bool(commit_override), component)
        return info
    if component in registry_sources["repositories"]:
        info["repository"] = component
        info["class"] = cls or idx.repository_class(registry_sources, component)
        path = idx.checkout(root, component)
        tag = f"v{version}"
        commit = idx.tag_commit(path, tag) if path is not None else None
        if path is not None and commit is None:
            warn(f"{component}: no tag {tag} in the checkout; nothing built from it")
        info.update(tag=tag if commit else None, tag_commit=commit, commit=commit)
        if commit:
            info["date"] = (idx.git(path, "for-each-ref", "--format=%(creatordate:short)", f"refs/tags/{tag}") or "").strip() or None
        kind = info["class"]
        if kind == "php-package":
            if commit and not registry_sources.get("exclude_known", True):
                warn(f"{component}: the paths the declaration excludes from archives are not known; no zip built")
            elif commit:
                package, requires = composer_info(path, tag, registry_sources.get("vendors"))
                payload = art.php_zip(path, tag, exclude)
                if payload is None:
                    warn(f"{component}: tag {tag} has no composer.json (or cannot be archived); no zip built")
                else:
                    info.update(package=package, requires=requires, archive=True)
                    info["payloads"].append((f"{component.split('/')[1]}-{version}.zip", payload))
        elif kind in ("runtime", "layer"):
            if files_dir is None or not files_dir.is_dir():
                warn(f"{component}: no --files directory with the released files; nothing copied")
            else:
                for entry in sorted(files_dir.iterdir()):
                    if entry.is_file() and not entry.is_symlink() and not entry.name.startswith("."):
                        info["payloads"].append((entry.name, entry))
                    else:
                        warn(f"{entry}: not a regular file; skipped")
        else:  # service, image-recipe of another repository, anything that is a tree
            tree_at(info, path, commit_override or commit, tree_path or "", bool(commit_override), component)
        return info
    sot = registry_sources["sot"]
    if not sot:
        warn(f"{component}: not a repository the publisher declares, and no SoT is declared; nothing published")
        return None
    info.update({"repository": sot})
    path = idx.checkout(root, sot)
    info["class"] = cls or idx.component_class(registry_sources, component)
    info["path"] = component
    if path is None:
        return info
    if idx.git(path, "cat-file", "-e", f"origin/HEAD:{component}/version.json") is None:
        warn(f"{component}: neither a declared repository nor a SoT component (no {component}/version.json on the "
             f"default branch of {sot}); nothing published")
        return None
    row = idx.sot_releases(path, component).get(version)
    row_tag, row_commit = (row or {}).get("tag"), (row or {}).get("commit")  # a tag row: the tag's own commit
    commit = commit_override or row_commit
    if row is None and not commit_override:
        warn(f"{component} {version}: no row in the release table of its README; nothing built")
    elif not commit:
        warn(f"{component} {version}: its row names no commit; nothing built (pass --commit)")
    elif row_tag and not row_commit:
        warn(f"{component} {version}: its row names tag {row_tag!r}, which this checkout does not have, so the "
             "commit of the release cannot be confirmed; nothing built")
    elif commit_override and row_tag and not idx.same_commit(commit_override, row_commit):
        raise art.Refusal(f"{component} {version}: --commit {commit_override[:12]} is not the commit of tag "
                          f"{row_tag!r} ({row_commit[:12]}); a tagged release is built from its tag, so other "
                          "bytes would be reported under the tag's commit")
    elif not is_ancestor(path, commit):
        warn(f"{component} {version}: {commit} is not on the default branch of {sot}; nothing built")
    else:
        moved = bool(commit_override) and bool(row_commit) and not idx.same_commit(commit_override, row_commit)
        info.update(commit=commit, date=(None if moved else (row or {}).get("date")) or idx.commit_date(path, commit),
                    tag=row_tag, tag_commit=row_commit if row_tag else None)
        info["requires"] = component_requires(path, commit, component)
        info["tree_at"] = (path, commit, component)
    return info


def tree_at(info: dict, path: Path | None, at: str | None, subtree: str, override: bool, component: str) -> None:
    """Record where the tree tar of a repository (or of one of its sub-components) is read: `at` of `path`."""
    info["path"] = subtree
    if path is None or not at:
        warn(f"{component}: no commit to read the tree at (no tag, no --commit); nothing built")
    elif override and info.get("tag_commit") and not idx.same_commit(at, info["tag_commit"]):
        raise art.Refusal(f"{component}: --commit {at[:12]} is not the commit of tag {info['tag']!r} "
                          f"({info['tag_commit'][:12]}); a tagged release is built from its tag, so other bytes "
                          "would be reported under the tag's commit")
    elif override and not is_ancestor(path, at):
        warn(f"{component}: {at} is not on the default branch; nothing built")
    else:
        info["commit"] = at
        info["tree_at"] = (path, at, subtree)


def lfs_ready(registry: Path, names: list[str]) -> str | None:
    """Why the files that must go through Git LFS cannot be committed from here, or None when they can."""
    needing = [n for n in names if art.is_lfs(Path(n).name)]
    if not needing:
        return None
    if idx.git(registry, "lfs", "version") is None:
        return "git lfs is not installed"
    for name in needing:
        out = idx.git(registry, "check-attr", "filter", "--", name) or ""
        if not out.strip().endswith(": lfs"):
            return f".gitattributes does not send {name} through Git LFS"
    return None


LFS_POINTER = re.compile(r"^version https://git-lfs\.github\.com/spec/v1\noid sha256:([0-9a-f]{64})\nsize (\d+)\n$")


def release_paths(destination: Path) -> tuple[list[Path], dict[str, dict]]:
    """The files of a release folder that belong to the release: its MANIFEST.json and every artifact it lists that
    is on disk, with each artifact's declared `{sha256, bytes}`. Anything else in the folder (a file a failed run
    left, a stray file) is not part of it and is never staged."""
    manifest_file = destination / "MANIFEST.json"
    manifest = idx.read_manifest(manifest_file) if manifest_file.is_file() else None
    if not manifest:
        return [], {}
    paths, declared = [manifest_file], {}
    for row in idx.manifest_artifacts(manifest, str(manifest_file)) or []:
        target = destination / row["name"]
        if target.is_file() and not target.is_symlink():
            paths.append(target)
            declared[row["name"]] = row
    return paths, declared


def lfs_pointer_problem(registry: Path, relative: str, row: dict) -> str | None:
    """Why the blob git would store for an LFS artifact is not the LFS pointer of its declared bytes, or None. The
    clean filter is run through `git hash-object --path` (as `git add` would), so nothing is staged to find out."""
    blob = idx.git(registry, "hash-object", "-w", f"--path={relative}", "--", relative)
    stored = idx.git(registry, "cat-file", "blob", blob.strip()) if blob else None
    if stored is None:
        return f"what git would store for {relative} cannot be read"
    match = LFS_POINTER.match(stored)
    if not match:
        return f"git would store {relative} as an ordinary blob, not as a Git LFS pointer"
    if match.group(1) != row.get("sha256") or (isinstance(row.get("bytes"), int) and int(match.group(2)) != row["bytes"]):
        return f"the Git LFS pointer of {relative} is not the one of the bytes its manifest declares"
    return None


def commit_release(registry: Path, index_path: Path | None, destination: Path, message: str) -> bool:
    """Stage and commit the index and the release (its manifest and the artifacts it lists), and nothing else.
    Every artifact that goes through Git LFS must become the LFS pointer of its declared bytes; otherwise nothing is
    staged or committed (it warns: the files stay written)."""
    if idx.git(registry, "rev-parse", "--show-toplevel") is None:
        warn(f"{registry} is not a git checkout; nothing staged or committed")
        return False
    files, declared = release_paths(destination)
    paths = ([index_path] if index_path is not None else []) + files
    if not paths:
        return False
    rel = [str(p.relative_to(registry)) for p in paths]
    lfs = {str((destination / n).relative_to(registry)): row for n, row in declared.items() if art.is_lfs(n)}
    reason = lfs_ready(registry, sorted(lfs))
    for relative, row in sorted(lfs.items()):
        reason = reason or lfs_pointer_problem(registry, relative, row)
    if reason:
        warn(f"not committed ({reason}); the files are written and not staged")
        return False
    if idx.git(registry, "add", "--", *rel) is None:
        warn("git add failed; nothing committed")
        return False
    done = subprocess.run(["git", "-C", str(registry), "commit", "-q", "-m", message, "--", *rel],
                              capture_output=True, text=True)
    if done.returncode != 0:
        warn("git commit did not commit (nothing changed, or no identity configured): " + (done.stderr or done.stdout).strip()[:200])
        return False
    return True


def payload_rows(info: dict) -> list[dict]:
    """`{name, sha256, bytes, src}` of every payload that can be read; a reserved or unreadable one warns."""
    listing = []
    for name, source in info["payloads"]:
        if name in RESERVED or name.startswith("."):
            warn(f"{name}: a name the release folder reserves for itself; skipped (it is never overwritten)")
            continue
        if isinstance(source, Path):
            try:
                listing.append({"name": name, "sha256": art.sha256_file(source), "bytes": source.stat().st_size, "src": source})
            except OSError as error:
                warn(f"{source}: cannot be read ({type(error).__name__}); skipped")
        else:
            listing.append({"name": name, "sha256": art.sha256_bytes(source), "bytes": len(source), "src": source})
    return listing


def tree_row(component: str, version: str, info: dict, exclude: list[str]) -> dict | None:
    repo_path, at, subtree = info["tree_at"]
    try:
        tree = art.tree_tar(repo_path, at, subtree, exclude)
    except ValueError as error:
        warn(f"{component}: {error}; no tree tar built")
        return None
    if tree is None:
        warn(f"{component}: cannot read the tree of {subtree or '.'} at {at}; no tree tar built")
        return None
    info["commit"] = tree["commit"]
    row = {"name": f"{idx.slug(component)}-{version}.tar", "sha256": art.sha256_bytes(tree["payload"]),
           "bytes": len(tree["payload"]), "files": tree["files"], "src": tree["payload"]}
    if tree["tree_digest"] is None:
        warn(f"{component}: the cli's collab.tree_digest is not installed; {row['name']} is published without a "
             "tree_digest (install the cli and publish again to add it)")
    else:
        row["tree_digest"] = tree["tree_digest"]
    return row


def bind_to_previous(component: str, version: str, destination: Path, listing: list[dict], old: dict) -> list[dict]:
    """A published version takes no other bytes; the stored bytes decide. The entries that may be (re)written:

    - a stored file with the same bytes: kept (when the manifest records another sha256 for it, that record is
      stale: it warns and the record is repaired);
    - a stored file with other bytes: a Refusal;
    - no stored file (or one that cannot be read) while the manifest records another well-formed sha256: the
      recorded binding is kept and this file is not written (it warns: nothing proves which bytes are right);
    - otherwise (a new file, or the recorded sha256 is this one): written.

    A row with the bytes its manifest records keeps what that row recorded (a tree_digest the cli is not here to
    compute again)."""
    keep = []
    for entry in listing:
        before = old.get(entry["name"])
        prior_sha = before.get("sha256") if before else None
        recorded = isinstance(prior_sha, str) and HEX64.match(prior_sha) is not None
        if before is not None and not recorded:
            warn(f"{component} {version}: the manifest records no usable sha256 for {entry['name']}; checking the stored file")
        stored, stored_sha = destination / entry["name"], None
        if stored.is_file() and not stored.is_symlink():
            try:
                stored_sha = art.sha256_file(stored)
            except OSError as error:
                warn(f"{stored}: cannot be read ({type(error).__name__}); its bytes are not compared")
        if stored_sha is not None and stored_sha != entry["sha256"]:
            raise art.Refusal(f"{component} {version}: the stored {entry['name']} has other bytes; "
                              "a published version takes no other bytes (cut a new version)")
        if recorded and prior_sha != entry["sha256"]:
            if stored_sha is None:
                warn(f"{component} {version}: the manifest records another sha256 for {entry['name']} and the stored "
                     "file cannot be compared; the recorded binding is kept and this file is not written")
                continue
            warn(f"{component} {version}: the manifest records a stale sha256 for {entry['name']}; the stored bytes "
                 "are these, so the record is repaired")
        elif recorded:
            for field in ("tree_digest", "files"):
                if field not in entry and field in before:
                    entry[field] = before[field]
        keep.append(entry)
    return keep


def reference_disagrees(previous: dict, info: dict) -> tuple[str, str] | None:
    """`(bound, now)` when this publication reads another commit than the one a published version's manifest is
    bound to. Both the tag's commit and the commit of the bytes are compared: a matching tag commit does not make
    other bytes the same release."""
    for field in ("tag_commit", "commit"):
        bound, now = previous.get(field), info.get(field)
        if isinstance(bound, str) and isinstance(now, str) and not idx.same_commit(bound, now):
            return bound, now
    return None


def stage_and_replace(destination: Path, listing: list[dict], manifest_text: str) -> list[Path]:
    """Write every file as `.<name>.part`, verify each staged file's sha256, check every destination can take a
    file, and only then replace the live files (the manifest last), keeping each replaced live file aside until all
    are in place. Any failure removes what was staged, puts every replaced live file back and leaves the release as
    it was (Unverified for a failure to write, Refusal for staged bytes that are not the source's)."""
    missing, d = [], destination
    while not d.exists():
        missing.append(d)
        d = d.parent
    staged: list[tuple[Path, Path]] = []
    replaced: list[tuple[Path, Path | None]] = []

    def clean_up():
        for target, backup in reversed(replaced):  # put the live release back as it was
            if backup is not None:
                os.replace(backup, target)
            else:
                target.unlink(missing_ok=True)
        for part, _ in staged:
            part.unlink(missing_ok=True)
        for folder in missing:  # deepest first: only the folders this run made, and only when empty
            try:
                folder.rmdir()
            except OSError:
                break

    try:
        destination.mkdir(parents=True, exist_ok=True)
        for entry in listing:
            part, target = destination / f".{entry['name']}.part", destination / entry["name"]
            staged.append((part, target))
            if isinstance(entry["src"], Path):
                shutil.copyfile(entry["src"], part)
            else:
                part.write_bytes(entry["src"])
            try:
                got = art.sha256_file(part)
            except OSError as error:
                raise Unverified(f"{part} cannot be read back ({type(error).__name__})") from error
            if got != entry["sha256"]:
                raise art.Refusal(f"{target} does not read back with the sha256 computed from its source")
        part, target = destination / ".MANIFEST.json.part", destination / "MANIFEST.json"
        staged.append((part, target))
        part.write_text(manifest_text, encoding="utf-8")
        if part.read_text(encoding="utf-8") != manifest_text:
            raise Unverified(f"{part} does not read back as written")
        for _, target in staged:  # preflight: every destination is absent or a regular file
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise Unverified(f"{target} is not a regular file; it cannot be replaced")
        for part, target in staged:
            backup = None
            if target.exists():
                backup = destination / f".{target.name}.prev"
                os.replace(target, backup)
            replaced.append((target, backup))
            os.replace(part, target)
    except (art.Refusal, Unverified):
        clean_up()
        raise
    except OSError as error:
        clean_up()
        raise Unverified(f"the release folder could not be updated ({type(error).__name__})") from error
    except BaseException:
        clean_up()
        raise
    for _, backup in replaced:
        if backup is not None:
            backup.unlink(missing_ok=True)
    return [target for _, target in staged]


def publish(registry: Path, root: Path, component: str, version: str, *, cls: str | None = None,
            files_dir: Path | None = None, tree_path: str | None = None, commit: str | None = None,
            requires: list[str] | None = None, commit_changes: bool = True,
            declaration: str | Path | None = None) -> int:
    """Exit status: 0 published or skipped (with warnings), 3 an actual byte mismatch (nothing live changed)."""
    version = version[1:] if isinstance(version, str) and version.startswith("v") else version
    if not idx.safe_component(component):
        warn(f"{component!r}: not a component key; nothing published")
        return 0
    if not idx.safe_segment(version):
        warn(f"{version!r}: not a version label that can name a folder; nothing published")
        return 0
    if cls is not None and cls not in idx.CLASSES:
        warn(f"--class {cls!r}: not a class ({', '.join(idx.CLASSES)}); the declared class is used")
        cls = None
    if tree_path and not idx.safe_component(tree_path):
        warn(f"--path {tree_path!r}: not a relative path inside the repository; no tree is built")
        tree_path, skip_tree = None, True
    else:
        skip_tree = False
    sources = idx.load_sources(registry / "sources.json", declaration, decl.cache_file(registry))
    if (component in sources["repositories"] and cls and tree_path and not skip_tree
            and cls != idx.repository_class(sources, component)):
        own = f"{component}/{tree_path.strip('/')}"
        warn(f"{component} --class {cls} --path {tree_path}: published as its own component {own} (class {cls}), "
             f"so the {idx.repository_class(sources, component)} releases of {component} stay as they are and both "
             f"are found in the index (declare it as `{own}: {cls}` in publication.classes)")
        component = own
    try:
        info = gather(sources, root, component, version, cls, files_dir, tree_path, commit)
    except art.Refusal as refusal:
        print(f"refused: {refusal}; nothing written", file=sys.stderr)
        return 3
    if info is None:
        print(f"{component} {version}: status=skipped; nothing published; {len(decl.WARNINGS)} warning(s)")
        return 0
    if skip_tree:
        info.pop("tree_at", None)
    if info.get("tree_at") and not sources.get("exclude_known", True):
        warn(f"{component}: the paths the declaration excludes from archives are not known; no tree tar built")
        info.pop("tree_at")
    artifacts_dir, index_path = registry / "artifacts", registry / "releases.json"
    destination = idx.release_dir(artifacts_dir, info["class"], component, version)
    written: list[Path] = []
    status = "skipped"
    exclude = sources.get("exclude") or []
    try:
        listing = payload_rows(info)
        if files_dir is not None and info["class"] in ("runtime", "layer") and listing:
            sums_check(files_dir, {e["name"]: e["sha256"] for e in listing})
            bound = bind_release_files(component, version, info["tag_commit"], listing)
            status = "skipped" if bound else "unverified"
            listing = bound
        if info.get("tree_at"):
            row = tree_row(component, version, info, exclude)
            if row is not None:
                listing.append(row)
        if (info.get("tree_at") or info.get("archive")) and listing and not exclude:
            idx.warn_once(f"no archive exclusion is declared (publication.archive_exclude in {decl.WHERE}); "
                          "the archive holds every file of its tree")
        previous = idx.read_manifest(destination / "MANIFEST.json") or {}
        rows = (idx.manifest_artifacts(previous, str(destination / "MANIFEST.json")) or []) if previous else []
        old = {a["name"]: a for a in rows}
        built = bool(listing)
        listing = bind_to_previous(component, version, destination, listing, old)
        disagreement = reference_disagrees(previous, info) if old else None
        if disagreement and listing:
            bound_to, now = disagreement
            warn(f"{component} {version}: already published from {bound_to[:12]}, and its source now names "
                 f"{now[:12]}; the published binding is kept and nothing is written (cut a new version)")
            listing = []
        elif listing:
            merged = {**old, **{e["name"]: {k: v for k, v in e.items() if k != "src"} for e in listing}}
            for artifact in merged.values():
                artifact["lfs"] = art.is_lfs(artifact["name"])
            manifest = {"schema": MANIFEST_SCHEMA, "component": component, "class": info["class"], "version": version,
                        "repository": info["repository"], "tag": info["tag"], "tag_commit": info["tag_commit"],
                        "commit": info["commit"], "date": info["date"], "package": info["package"],
                        "path": info["path"] if info.get("tree_at") else None,
                        "requires": info["requires"] + extra_requires(requires),
                        "artifacts": [merged[n] for n in sorted(merged)]}
            manifest = {k: v for k, v in manifest.items() if v is not None}
            try:
                written = stage_and_replace(destination, listing,
                                            json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
                status = "ok"
            except Unverified as problem:
                warn(f"{component} {version}: {problem}; nothing replaced, the release stays as it was")
                status = "unverified"
        elif built:
            warn(f"{component} {version}: nothing to write; the published release stays as it was")
        elif status != "unverified":
            warn(f"{component} {version}: no artifact could be built; no MANIFEST.json written")
    except art.Refusal as refusal:
        print(f"refused: {refusal}; no live file changed", file=sys.stderr)
        return 3

    components, state = idx.load_index(index_path)
    index_written = False
    if state == "unreadable":
        warn(f"{index_path}: not rewritten, so nothing of it is lost; repair it or rebuild it with "
             "scripts/build_index.py (which keeps a copy of it first)")
    else:
        entry = idx.read_component(root, sources, component, artifacts_dir, components)
        if entry is None:
            warn(f"{component}: could not read its releases; the previous index entry is kept")
        else:
            components[component] = idx.merge_history(component, entry, components.get(component), artifacts_dir)
        components = {k: (e if idx.usable_entry(k, e) and "class" in e else idx.upgrade_entry(k, e, artifacts_dir))
                      for k, e in components.items()}
        idx.write_index(index_path, components)
        index_written = True
    where = f"{len(written) - 1} artifact(s) in {destination}" if written else "no artifact published"
    print(f"{component} {version}: status={status}; {where}; index {index_path if index_written else 'unchanged'}; "
          f"{len(decl.WARNINGS)} warning(s)")
    if commit_changes and (index_written or destination.exists()):
        if commit_release(registry, index_path if index_written else None, destination, f"Publicación: {component} {version}"):
            print("committed")
    return 0


def extra_requires(given: list[str] | None) -> list[dict]:
    """`--requires COMPONENT=CONSTRAINT` rows; one without a component warns and is ignored."""
    rows = []
    for text in given or []:
        name, _, constraint = text.partition("=")
        if not name.strip():
            warn(f"--requires {text!r}: no component; ignored")
            continue
        rows.append({"kind": "component", "name": name.strip(), "constraint": constraint})
    return rows


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    parser = decl.Parser(description=__doc__.split("\n\n")[0])
    parser.add_argument("component", nargs="?", help="owner/repo of a declared repository, or a SoT component path")
    parser.add_argument("version", nargs="?", help="the release label, X.Y.Z (a leading v is dropped)")
    parser.add_argument("--declaration", help=f"the organization's declaration (default: ${decl.ENV}); without it, sources.json is read")
    parser.add_argument("--registry", type=Path, default=here, help="the checkout of this repository (default: this one)")
    parser.add_argument("--root", type=Path, default=here.parent, help="directory holding the checkouts (default: next to this repo)")
    parser.add_argument("--files", type=Path, help="runtime and layer: the directory with the released files")
    parser.add_argument("--class", dest="cls", help=f"override the declared class ({', '.join(idx.CLASSES)})")
    parser.add_argument("--path", help="tree classes of a repository: the directory to pack (default: all)")
    parser.add_argument("--commit", help="tree classes: the commit to pack, instead of the row's or the tag's")
    parser.add_argument("--requires", action="append", default=[], metavar="COMPONENT=CONSTRAINT",
                        help="a dependency to record in the manifest (repeatable)")
    parser.add_argument("--no-commit", action="store_true", help="write the files, stage and commit nothing")
    try:
        args, unknown = parser.parse_known_args(argv)
    except decl.UsageWarning as problem:
        warn(f"{problem}; nothing published (see --help)")
        return 0
    for extra in unknown:
        warn(f"unknown argument {extra!r}; ignored")
    if not args.component or not args.version:
        warn("a component and a version are needed to know what to publish; nothing published (see --help)")
        return 0
    return publish(args.registry, args.root, args.component, args.version, cls=args.cls, files_dir=args.files,
                   tree_path=args.path, commit=args.commit, requires=args.requires, commit_changes=not args.no_commit,
                   declaration=args.declaration)


if __name__ == "__main__":
    sys.exit(main())
