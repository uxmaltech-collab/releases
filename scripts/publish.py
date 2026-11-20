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
  `*.component.json`, `*SHA256SUMS`, `install.py`, lock, manifest, notes). The sums files are checked.
- sot-component, image-recipe: the tree tar of the component's directory at the commit of its row in the
  README release table (`--commit` overrides), with its `tree_digest` (from the installed cli).
- service, and an image-recipe of a repository other than the SoT: the tree tar of `--path` (default: the whole
  tree) at the tag's commit or `--commit`.

The authority of a release is its upstream reference, `repository` + `tag_commit` (or the row's `commit`),
recorded in the manifest and in the index; the bytes stored here are a verified cache of it. Archives never carry
the paths the declaration lists in `publication.archive_exclude` (the organization's own declarations).

Only what is published is read: tags, `origin/HEAD` and the objects of a checkout, never a working tree. What
cannot be read (an undeclared component, no checkout, no tag, no `composer.json`, no `--files`, an unreadable
file, a malformed sums line, a commit that is not on the default branch) warns on stderr and is skipped; the
index is rewritten anyway, keeping every version it had. Every byte is staged and verified before any live file
is replaced. The only refusal is an actual byte mismatch: a well-formed sum that is not the file's, a version
already published with other bytes, a copy or a tar that does not read back as built. Then no live file changes
and the exit status is 3. Everything else exits 0.

It ends with `git add` and one commit of `releases.json` and the release folder, unless `--no-commit` (or when
the files that must go through Git LFS cannot: no `git lfs`, or `.gitattributes` does not cover them).
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


def sums_check(directory: Path, digests: dict[str, str]) -> None:
    """A `SHA256SUMS` file lists `<sha256>  <name>`. A well-formed sum that is not the listed file's is a Refusal
    (the bytes are not the released ones); a malformed line, an unreadable sums file or a file that is not in the
    directory warns and is not checked."""
    for name in sorted(digests):
        if not (name == "SHA256SUMS" or name.endswith(".SHA256SUMS")):
            continue
        try:
            text = (directory / name).read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            warn(f"{name}: cannot be read ({type(error).__name__}); its sums are not checked")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            parts = line.strip().split(None, 1)
            if not parts or parts[0].startswith("#"):
                continue
            if len(parts) != 2:
                warn(f"{name}:{number}: not `<sha256>  <name>`; not checked")
                continue
            listed, target = parts[0].lower(), parts[1].lstrip("*").strip()
            if not HEX64.match(listed):
                warn(f"{name}:{number}: the sum for {target} is not a SHA-256; not checked")
            elif target not in digests:
                warn(f"{name} lists {target}, which is not in the directory")
            elif digests[target] != listed:
                raise art.Refusal(f"{name} lists another sha256 for {target} than the file has")


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
            if commit:
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
            info["path"] = tree_path or ""
            at = commit_override or commit
            if path is None or not at:
                warn(f"{component}: no commit to read the tree at (no tag, no --commit); nothing built")
            elif commit_override and not is_ancestor(path, at):
                warn(f"{component}: {at} is not on the default branch; nothing built")
            else:
                info["commit"] = at
                info["tree_at"] = (path, at, info["path"])
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
    commit = commit_override or (row or {}).get("commit")
    if row is None and not commit_override:
        warn(f"{component} {version}: no row in the release table of its README; nothing built")
    elif not commit:
        warn(f"{component} {version}: its row names no commit; nothing built (pass --commit)")
    elif not is_ancestor(path, commit):
        warn(f"{component} {version}: {commit} is not on the default branch of {sot}; nothing built")
    else:
        info.update(commit=commit, date=(row or {}).get("date") or idx.commit_date(path, commit),
                    tag=(row or {}).get("tag"), tag_commit=(row or {}).get("tag_commit"))
        info["requires"] = component_requires(path, commit, component)
        info["tree_at"] = (path, commit, component)
    return info


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


def commit_release(registry: Path, paths: list[Path], lfs_names: list[str], message: str) -> bool:
    if idx.git(registry, "rev-parse", "--show-toplevel") is None:
        warn(f"{registry} is not a git checkout; nothing staged or committed")
        return False
    reason = lfs_ready(registry, lfs_names)
    if reason:
        warn(f"not committed ({reason}); the files are written and not staged")
        return False
    rel = [str(p.relative_to(registry)) for p in paths]
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


def bind_to_previous(component: str, version: str, destination: Path, listing: list[dict], old: dict) -> None:
    """A published version takes no other bytes. A prior row with a well-formed sha256 that differs, or (with no
    usable prior sha256) a stored file whose bytes differ, is a Refusal. Missing evidence warns. A row with the same
    bytes keeps what the prior row recorded (a tree_digest the cli is not here to compute again)."""
    for entry in listing:
        before = old.get(entry["name"])
        prior_sha = before.get("sha256") if before else None
        if isinstance(prior_sha, str) and HEX64.match(prior_sha):
            if prior_sha != entry["sha256"]:
                raise art.Refusal(f"{component} {version}: {entry['name']} is already published with another sha256; "
                                  "a published version takes no other bytes (cut a new version)")
            for field in ("tree_digest", "files"):
                if field not in entry and field in before:
                    entry[field] = before[field]
            continue
        if before is not None:
            warn(f"{component} {version}: the manifest records no usable sha256 for {entry['name']}; checking the stored file")
        stored = destination / entry["name"]
        if stored.is_file():
            try:
                stored_sha = art.sha256_file(stored)
            except OSError as error:
                warn(f"{stored}: cannot be read ({type(error).__name__}); its bytes are not compared")
                continue
            if stored_sha != entry["sha256"]:
                raise art.Refusal(f"{component} {version}: the stored {entry['name']} has other bytes; "
                                  "a published version takes no other bytes (cut a new version)")


def stage_and_replace(destination: Path, listing: list[dict], manifest_text: str) -> list[Path]:
    """Write every file as `.<name>.part`, verify each staged file's sha256, and only then replace the live files
    (the manifest last). Any failure removes what was staged and leaves every live file as it was."""
    missing, d = [], destination
    while not d.exists():
        missing.append(d)
        d = d.parent
    destination.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    try:
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
    except BaseException:
        for part, _ in staged:
            part.unlink(missing_ok=True)
        for folder in missing:  # deepest first: only the folders this run made, and only when empty
            try:
                folder.rmdir()
            except OSError:
                break
        raise
    for part, target in staged:
        os.replace(part, target)
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
    sources = idx.load_sources(registry / "sources.json", declaration)
    info = gather(sources, root, component, version, cls, files_dir, tree_path, commit)
    if info is None:
        print(f"{component} {version}: status=skipped; nothing published; {len(decl.WARNINGS)} warning(s)")
        return 0
    if skip_tree:
        info.pop("tree_at", None)
    artifacts_dir, index_path = registry / "artifacts", registry / "releases.json"
    destination = idx.release_dir(artifacts_dir, info["class"], component, version)
    written: list[Path] = []
    status = "skipped"
    exclude = sources.get("exclude") or []
    try:
        listing = payload_rows(info)
        if info.get("tree_at"):
            row = tree_row(component, version, info, exclude)
            if row is not None:
                listing.append(row)
        if (info.get("tree_at") or info.get("archive")) and listing and not exclude:
            idx.warn_once(f"no archive exclusion is declared (publication.archive_exclude in {decl.WHERE}); "
                          "the archive holds every file of its tree")
        if files_dir is not None and info["class"] in ("runtime", "layer"):
            sums_check(files_dir, {e["name"]: e["sha256"] for e in listing})
        previous = idx.read_manifest(destination / "MANIFEST.json") or {}
        rows = (idx.manifest_artifacts(previous, str(destination / "MANIFEST.json")) or []) if previous else []
        old = {a["name"]: a for a in rows}
        bind_to_previous(component, version, destination, listing, old)
        if listing:
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
        else:
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
            components[component] = idx.merge_history(component, entry, components.get(component))
        components = {k: (e if idx.usable_entry(k, e) and "class" in e else idx.upgrade_entry(k, e, artifacts_dir))
                      for k, e in components.items()}
        idx.write_index(index_path, components)
        index_written = True
    where = f"{len(written) - 1} artifact(s) in {destination}" if written else "no artifact published"
    print(f"{component} {version}: status={status}; {where}; index {index_path if index_written else 'unchanged'}; "
          f"{len(decl.WARNINGS)} warning(s)")
    if commit_changes:
        paths = ([index_path] if index_written else []) + ([destination] if destination.exists() else [])
        lfs_names = [str(p.relative_to(registry)) for p in written]
        if paths and commit_release(registry, paths, lfs_names, f"Publicación: {component} {version}"):
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
