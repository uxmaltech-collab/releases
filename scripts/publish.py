#!/usr/bin/env python3
"""Publish one release of one component: build its artifacts from the tag, write MANIFEST.json, update the index.

    python3 -I scripts/publish.py <component> <version> [--root DIR] [--files DIR] [--no-commit] ...

`<component>` is the key the index uses: `owner/repo` of a repository listed in `sources.json`, or the path of a
SoT component (`skills/human-artifact-authoring`, `containers/agent-base`). `<version>` is `X.Y.Z` (a leading `v`
is dropped). It is the last row of each release procedure: run it after the tag (or, for the SoT, after the
version PR is merged), with every checkout fetched in `--root`.

What it builds, by class (`sources.json` says which; `--class` overrides):

- php-package: a stored zip of `git archive` of the tag `vX.Y.Z`, which must hold `composer.json`.
- runtime, layer: the released files of the version, copied from the directory given with `--files` (wheels,
  `*.component.json`, `*SHA256SUMS`, `install.py`, lock, manifest, notes). The sums files are checked.
- sot-component, image-recipe: the tree tar of the component's directory at the commit of its row in the
  README release table (`--commit` overrides), with its `tree_digest`.
- service, and an image-recipe of a repository other than the SoT: the tree tar of `--path` (default: the whole
  tree) at the tag's commit or `--commit`.

Only what is published is read: tags, `origin/HEAD` and the objects of a checkout, never a working tree. What
cannot be read (no checkout, no tag, no `composer.json`, no `--files`, a commit that is not on the default
branch) is reported on stderr and skipped; the index is rewritten anyway. The only refusal is a mismatch of a
sha256 or a digest: a sums file that disagrees with its file, a version already published with other bytes, a
copy or a tar that does not read back as computed. Then nothing is written and the exit status is 3.

It ends with `git add` and one commit of `releases.json` and the release folder, unless `--no-commit` (or when
the files that must go through Git LFS cannot: no `git lfs`, or `.gitattributes` does not cover them).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # `python3 -I` does not put the script's folder there

import artifacts as art  # noqa: E402
import build_index as idx  # noqa: E402
from build_index import warn  # noqa: E402

MANIFEST_SCHEMA = "uxmaltech-collab.release-manifest/v1"
OWN_VENDORS = ("uxmaltech", "uxmaltech-collab")


def is_ancestor(path: Path, commit: str) -> bool:
    return idx.git(path, "merge-base", "--is-ancestor", commit, "origin/HEAD") is not None


def read_json_at(path: Path, commit: str, name: str) -> dict | None:
    try:
        data = json.loads(idx.git(path, "show", f"{commit}:{name}") or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def composer_info(path: Path, tag: str) -> tuple[str | None, list[dict]]:
    data = read_json_at(path, f"refs/tags/{tag}", "composer.json") or {}
    name = data.get("name") if isinstance(data.get("name"), str) else None
    required = data.get("require") if isinstance(data.get("require"), dict) else {}
    own = [{"kind": "composer", "name": n, "constraint": str(c)} for n, c in sorted(required.items())
           if "/" in n and n.split("/")[0] in OWN_VENDORS]
    return name, own


def component_requires(path: Path, commit: str, component: str) -> list[dict]:
    """The SoT component or layer versions a `version.json` pins: each object value with `component` and `version`."""
    meta = read_json_at(path, commit, f"{component}/version.json") or {}
    return [{"kind": "component", "name": v["component"], "constraint": v["version"]}
            for _, v in sorted(meta.items())
            if isinstance(v, dict) and isinstance(v.get("component"), str) and isinstance(v.get("version"), str)]


def sums_check(directory: Path, digests: dict[str, str]) -> None:
    """A `SHA256SUMS` file lists `<sha256>  <name>`: a listed file whose sha256 differs is a Refusal."""
    for name in sorted(digests):
        if not (name == "SHA256SUMS" or name.endswith(".SHA256SUMS")):
            continue
        for line in (directory / name).read_text(errors="replace").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            listed, target = parts[0].lower(), parts[1].lstrip("*").strip()
            if target in digests and digests[target] != listed:
                raise art.Refusal(f"{name} lists another sha256 for {target} than the file has")
            if target not in digests:
                warn(f"{name} lists {target}, which is not in the directory")


def gather(registry_sources: dict, root: Path, component: str, version: str, cls: str | None, files_dir: Path | None,
           tree_path: str | None, commit_override: str | None) -> dict | None:
    """What can be read about this release: `{repository, class, tag, tag_commit, commit, date, path, package,
    requires, payloads: [(name, bytes | Path)], tree: {...}|None}`. None is a usage error."""
    info = {"component": component, "version": version, "tag": None, "tag_commit": None, "commit": None,
            "date": None, "path": None, "package": None, "requires": [], "payloads": [], "tree_digest": None}
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
                package, requires = composer_info(path, tag)
                payload = art.php_zip(path, tag)
                if payload is None:
                    warn(f"{component}: tag {tag} has no composer.json; no zip built")
                else:
                    info.update(package=package, requires=requires)
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
        return None
    info.update({"repository": sot})
    path = idx.checkout(root, sot)
    info["class"] = cls or idx.sot_class(component)
    info["path"] = component
    if path is None:
        return info
    if idx.git(path, "cat-file", "-e", f"origin/HEAD:{component}/version.json") is None:
        print(f"error: {component} is neither a repository of sources.json nor a SoT component", file=sys.stderr)
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


def publish(registry: Path, root: Path, component: str, version: str, *, cls: str | None = None,
            files_dir: Path | None = None, tree_path: str | None = None, commit: str | None = None,
            requires: list[str] | None = None, commit_changes: bool = True) -> int:
    """Exit status: 0 published (or skipped with warnings), 2 usage error, 3 a sha256 or digest refusal."""
    version = version[1:] if version.startswith("v") else version
    sources = idx.load_sources(registry / "sources.json")
    info = gather(sources, root, component, version, cls, files_dir, tree_path, commit)
    if info is None:
        return 2
    artifacts_dir, index_path = registry / "artifacts", registry / "releases.json"
    destination = idx.release_dir(artifacts_dir, info["class"], component, version)
    written: list[Path] = []
    listing: list[dict] = []
    try:
        for name, source in info["payloads"]:
            if isinstance(source, Path):
                listing.append({"name": name, "sha256": art.sha256_file(source), "bytes": source.stat().st_size, "src": source})
            else:
                listing.append({"name": name, "sha256": art.sha256_bytes(source), "bytes": len(source), "src": source})
        if info.get("tree_at"):
            repo_path, at, subtree = info["tree_at"]
            try:
                tree = art.tree_tar(repo_path, at, subtree)
            except ValueError as error:
                warn(f"{component}: {error}; no tree tar built")
                tree = None
            if tree is None:
                warn(f"{component}: cannot read the tree of {subtree or '.'} at {at}; no tree tar built")
            else:
                info["commit"] = tree["commit"]
                listing.append({"name": f"{idx.slug(component)}-{version}.tar", "sha256": art.sha256_bytes(tree["payload"]),
                                "bytes": len(tree["payload"]), "tree_digest": tree["tree_digest"], "files": tree["files"],
                                "src": tree["payload"]})
        if files_dir is not None and info["class"] in ("runtime", "layer"):
            sums_check(files_dir, {e["name"]: e["sha256"] for e in listing})
        previous = idx.read_manifest(destination / "MANIFEST.json") or {}
        old = {a["name"]: a for a in previous.get("artifacts", []) if isinstance(a, dict) and "name" in a}
        for entry in listing:
            before = old.get(entry["name"])
            if before and before.get("sha256") != entry["sha256"]:
                raise art.Refusal(f"{component} {version}: {entry['name']} is already published with another sha256; "
                                  "a published version takes no other bytes (cut a new version)")
        if listing:
            destination.mkdir(parents=True, exist_ok=True)
            for entry in listing:
                target, part = destination / entry["name"], destination / f".{entry['name']}.part"
                if isinstance(entry["src"], Path):
                    shutil.copyfile(entry["src"], part)
                else:
                    part.write_bytes(entry["src"])
                os.replace(part, target)
                if art.sha256_file(target) != entry["sha256"]:
                    raise art.Refusal(f"{target} does not read back with the sha256 computed from its source")
                written.append(target)
            merged = {**old, **{e["name"]: {k: v for k, v in e.items() if k != "src"} for e in listing}}
            for artifact in merged.values():
                artifact["lfs"] = art.is_lfs(artifact["name"])
            manifest = {"schema": MANIFEST_SCHEMA, "component": component, "class": info["class"], "version": version,
                        "repository": info["repository"], "tag": info["tag"], "tag_commit": info["tag_commit"],
                        "commit": info["commit"], "date": info["date"], "package": info["package"],
                        "path": info["path"] if info.get("tree_at") else None,
                        "requires": info["requires"] + [{"kind": "component", "name": n.partition("=")[0],
                                                         "constraint": n.partition("=")[2]} for n in (requires or [])],
                        "artifacts": [merged[n] for n in sorted(merged)]}
            manifest = {k: v for k, v in manifest.items() if v is not None}
            (destination / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            written.append(destination / "MANIFEST.json")
        else:
            warn(f"{component} {version}: no artifact could be built; no MANIFEST.json written")
    except art.Refusal as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 3

    components = idx.read_index(index_path)
    entry = idx.read_component(root, sources, component, artifacts_dir)
    if entry is None:
        warn(f"{component}: could not read its releases; the previous index entry is kept")
    else:
        components[component] = entry
    components = {k: (e if "class" in e else idx.upgrade_entry(k, e, artifacts_dir)) for k, e in components.items()}
    idx.write_index(index_path, components)
    where = f"{len(listing)} artifact(s) in {destination}" if listing else "no artifact published"
    print(f"{component} {version}: {where}; index {index_path}")
    if commit_changes:
        paths = [index_path] + ([destination] if destination.exists() else [])
        lfs_names = [str(p.relative_to(registry)) for p in written]
        if commit_release(registry, paths, lfs_names, f"Publicación: {component} {version}"):
            print("committed")
    return 0


def main() -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("component", help="owner/repo of a repository in sources.json, or a SoT component path")
    parser.add_argument("version", help="X.Y.Z (a leading v is dropped)")
    parser.add_argument("--registry", type=Path, default=here, help="the checkout of this repository (default: this one)")
    parser.add_argument("--root", type=Path, default=here.parent, help="directory holding the checkouts (default: next to this repo)")
    parser.add_argument("--files", type=Path, help="runtime and layer: the directory with the released files")
    parser.add_argument("--class", dest="cls", choices=idx.CLASSES, help="override the class of sources.json")
    parser.add_argument("--path", help="tree classes of a repository: the directory to pack (default: all)")
    parser.add_argument("--commit", help="tree classes: the commit to pack, instead of the row's or the tag's")
    parser.add_argument("--requires", action="append", default=[], metavar="COMPONENT=CONSTRAINT",
                        help="a dependency to record in the manifest (repeatable)")
    parser.add_argument("--no-commit", action="store_true", help="write the files, stage and commit nothing")
    args = parser.parse_args()
    return publish(args.registry, args.root, args.component, args.version, cls=args.cls, files_dir=args.files,
                   tree_path=args.path, commit=args.commit, requires=args.requires, commit_changes=not args.no_commit)


if __name__ == "__main__":
    sys.exit(main())
