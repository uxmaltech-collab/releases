"""Tests of the index builder and the publisher, on temporary git repositories. Standard library only.

    python3 -I -m unittest
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import artifacts as art  # noqa: E402
import build_index as idx  # noqa: E402
import publish as pub  # noqa: E402



def documented_digest(entries):
    """The tree digest as the contract writes it: what the fake cli module below answers."""
    digest = hashlib.sha256()
    for relative, content in sorted(entries, key=lambda e: e[0].split("/")):
        if "__pycache__" not in relative.split("/"):
            digest.update(relative.encode() + b"\0" + hashlib.sha256(content() if callable(content) else content).digest())
    return digest.hexdigest()


@contextlib.contextmanager
def cli_installed():
    """An obvious fake of the installed cli's `collab.tree_digest` (the real one is not on this machine's path)."""
    package, module = types.ModuleType("collab"), types.ModuleType("collab.tree_digest")
    module.tree_digest = documented_digest
    package.tree_digest = module
    with mock.patch.dict(sys.modules, {"collab": package, "collab.tree_digest": module}):
        yield


@contextlib.contextmanager
def cli_absent():
    with mock.patch.dict(sys.modules, {"collab": None, "collab.tree_digest": None}):
        yield


IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(repo: Path, *args: str, date: str = "2026-01-02T03:04:05Z") -> str:
    env = {**os.environ, **IDENTITY, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True)
    return done.stdout.strip()


class Repo:
    """A checkout as the scripts expect it: a default branch published as origin/HEAD."""

    def __init__(self, root: Path, name: str, owner: str | None = "example-org", folder: Path | None = None):
        self.path = folder or root / name
        self.path.mkdir(parents=True)
        git(self.path, "init", "-q", "-b", "main")
        git(self.path, "config", "commit.gpgsign", "false")
        git(self.path, "config", "tag.gpgsign", "false")
        if owner:  # the checkout's identity: what its origin remote names (never fetched: no network)
            git(self.path, "remote", "add", "origin", f"https://example.test/{owner}/{name}.git")

    def commit(self, files: dict[str, str | bytes], message: str = "c", date: str = "2026-01-02T03:04:05Z") -> str:
        for name, content in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if isinstance(content, bytes) else content.encode())
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message, date=date)
        return git(self.path, "rev-parse", "HEAD")

    def tag(self, name: str, message: str | None = None, date: str = "2026-01-02T03:04:05Z") -> None:
        git(self.path, *(["tag", "-a", name, "-m", message] if message else ["tag", name]), date=date)

    def publish_head(self) -> None:
        git(self.path, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "checkouts"
        self.root.mkdir()
        self.registry = self.tmp / "releases"
        self.registry.mkdir()
        self.old_env = {k: os.environ.get(k) for k in (*IDENTITY, "COLLAB_RELEASES_DECLARATION")}
        os.environ.update(IDENTITY)
        os.environ.pop("COLLAB_RELEASES_DECLARATION", None)
        idx._WARNED.clear()
        quiet = contextlib.redirect_stderr(io.StringIO())  # warnings of calls a test does not inspect
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        pub.decl.WARNINGS.clear()
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def sources(self, **kw):
        data = {"schema": "collab.release-sources/v2", "sot": None, "repositories": [], "classes": {}}
        data.update(kw)
        (self.registry / "sources.json").write_text(json.dumps(data))

    def declaration(self, publication=None, **top) -> Path:
        data = {"schema": "collab.release-adoption-declaration/v1", **top}
        if publication is not None:
            data["publication"] = publication
        path = self.tmp / "release-adoption.v1.json"
        path.write_text(json.dumps(data))
        return path

    def run_quiet(self, fn, *args, **kw):
        """(result, stderr) of a call, with stdout hidden."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            result = fn(*args, **kw)
        return result, err.getvalue()

    def index(self) -> dict:
        return json.loads((self.registry / "releases.json").read_text())

    def sot(self, rows: str, version="0.1.2") -> Repo:
        repo = Repo(self.root, "sot", owner="o")
        self.first = repo.commit({"containers/recipe/version.json": json.dumps({"component": "containers/recipe", "version": version,
                                                                              "released": "2026-02-03"}),
                                  "containers/recipe/README.md": "# r\n\n| Version | Commit | Notes |\n| --- | --- | --- |\n" + rows})
        return repo


class IndexV1Compatible(Case):
    def test_v1_fields_stay_and_v2_fields_are_added(self):
        repo = Repo(self.root, "example-sre")
        repo.commit({"CHANGELOG.md": "# c\n\n## 0.2.0\n\nSecond.\n\n## 0.1.0\n\nFirst.\n"}, date="2026-03-01T00:00:00Z")
        repo.tag("v0.1.0", "first")
        sha = repo.commit({"x": "1"}, date="2026-04-01T00:00:00Z")
        repo.tag("v0.2.0", "second", date="2026-04-01T00:00:00Z")
        repo.publish_head()
        self.sources(repositories=["example-org/example-sre"])
        components = idx.build(self.root, idx.load_sources(self.registry / "sources.json"), self.registry / "artifacts")
        idx.write_index(self.registry / "releases.json", components)
        data = self.index()
        self.assertEqual(data["schema"], "collab.releases/v2")
        entry = data["components"]["example-org/example-sre"]
        # What a v1 reader (PublicReleases::read) uses.
        self.assertEqual((entry["repository"], entry["kind"], entry["latest"]), ("example-org/example-sre", "repository", "0.2.0"))
        newest, oldest = entry["releases"]
        self.assertEqual((newest["version"], newest["date"], newest["changelog"]), ("0.2.0", "2026-04-01", "Second."))
        self.assertEqual((oldest["version"], oldest["changelog"]), ("0.1.0", "First."))
        self.assertEqual(newest["commit"], sha)
        # What v2 adds.
        self.assertEqual((newest["class"], newest["tag_commit"], newest["artifacts"], newest["requires"]), ("layer", sha, [], []))

    def test_v1_sources_file_still_reads_as_layers(self):
        (self.registry / "sources.json").write_text(json.dumps(
            {"schema": "example-org.release-sources/v1", "sot": "o/sot", "repositories": ["o/a-sre"]}))
        sources = idx.load_sources(self.registry / "sources.json")
        self.assertEqual(idx.repository_class(sources, "o/a-sre"), "layer")


class TagRow(Case):
    def test_row_written_as_tag_gets_the_tag_commit(self):
        repo = self.sot("| 0.1.2 | tag `recipe-v0.1.2` | Third. |\n| 0.1.1 | tag `recipe-v0.1.1` | Second. |\n"
                        "| 0.1.0 | `{first}` | First. |\n")
        # the table is written before its own commit exists, so rewrite with the real hash after tagging
        tagged = repo.commit({"containers/recipe/Dockerfile": "FROM scratch\n"}, date="2026-02-02T00:00:00Z")
        repo.tag("recipe-v0.1.1", date="2026-02-02T00:00:00Z")
        later = repo.commit({"containers/recipe/Dockerfile": "FROM scratch\nRUN true\n"}, date="2026-02-03T00:00:00Z")
        repo.tag("recipe-v0.1.2", "annotated", date="2026-02-03T00:00:00Z")
        readme = (repo.path / "containers/recipe/README.md").read_text().replace("{first}", self.first[:12])
        repo.commit({"containers/recipe/README.md": readme}, date="2026-02-04T00:00:00Z")
        repo.publish_head()
        self.sources(sot="o/sot")
        entry = idx.sot_components("o/sot", repo.path)["containers/recipe"]
        by = {r["version"]: r for r in entry["releases"]}
        self.assertEqual(by["0.1.2"]["commit"], later)
        self.assertEqual(by["0.1.2"]["tag_commit"], later)  # an annotated tag resolves to its commit
        self.assertEqual(by["0.1.2"]["date"], "2026-02-03")
        self.assertEqual(by["0.1.1"]["commit"], tagged)
        self.assertEqual(by["0.1.0"]["commit"], self.first[:12])  # a commit cell is kept as written (v1)
        self.assertIsNone(by["0.1.0"]["tag_commit"])
        self.assertEqual(entry["class"], "image-recipe")
        self.assertTrue(all(r["commit"] for r in entry["releases"]))

    def test_regex_accepts_both_shapes(self):
        self.assertEqual(idx.ROW.match("| 0.1.2 | tag `x-v0.1.2` | t |").groups(), ("0.1.2", "x-v0.1.2", None, "t"))
        self.assertEqual(idx.ROW.match("| `v1.0.0` | `abcdef1` | t |").groups(), ("1.0.0", None, "abcdef1", "t"))
        self.assertEqual(idx.ROW.match("| 1.0.0 |  | t |").groups(), ("1.0.0", None, None, "t"))
        self.assertIsNone(idx.ROW.match("| Version | Commit | Notes |"))

    def test_unknown_tag_warns_and_leaves_commit_null(self):
        repo = self.sot("| 0.1.2 | tag `missing-v0.1.2` | Third. |\n")
        repo.publish_head()
        entry, err = self.run_quiet(idx.sot_components, "o/sot", repo.path)
        self.assertIn("missing-v0.1.2", err)
        self.assertIsNone(entry["containers/recipe"]["releases"][0]["commit"])


class TreeTar(Case):
    FILES = {"a/x": "1", "a.txt": "2", "a-b": "3", "a.json": "4", "run.sh": "#!/bin/sh\n", "p/__pycache__/m.pyc": "zz"}

    def repo(self):
        repo = Repo(self.root, "r")
        sha = repo.commit(self.FILES, date="2026-05-06T07:08:09Z")
        git(repo.path, "update-index", "--chmod=+x", "run.sh")
        git(repo.path, "commit", "-q", "--amend", "-m", "c", date="2026-05-06T07:08:09Z")
        return repo, git(repo.path, "rev-parse", "HEAD")

    def test_deterministic_ustar_sorted_by_component(self):
        repo, sha = self.repo()
        one, two = art.tree_tar(repo.path, sha), art.tree_tar(repo.path, sha)
        self.assertEqual(one["payload"], two["payload"])
        with tarfile.open(fileobj=io.BytesIO(one["payload"])) as tar:
            members = tar.getmembers()
        self.assertEqual([m.name for m in members], ["a/x", "a-b", "a.json", "a.txt", "run.sh"])  # no __pycache__
        self.assertTrue(all(m.uid == 0 and m.gid == 0 and m.uname == "" and m.gname == "" for m in members))
        self.assertEqual(one["payload"][257:263], b"ustar\x00")  # USTAR, and the first entry is a file: no pax header
        self.assertEqual(one["payload"][156:157], b"0")
        self.assertEqual({m.mtime for m in members}, {int(git(repo.path, "show", "-s", "--format=%ct", sha))})
        self.assertEqual([m.mode for m in members], [0o644, 0o644, 0o644, 0o644, 0o755])
        self.assertEqual(one["files"], 5)

    def test_tree_digest_is_the_documented_one(self):
        import hashlib
        repo, sha = self.repo()
        expected = hashlib.sha256()
        for name, data in (("a/x", "1"), ("a-b", "3"), ("a.json", "4"), ("a.txt", "2"), ("run.sh", "#!/bin/sh\n")):
            expected.update(name.encode() + b"\0" + hashlib.sha256(data.encode()).digest())
        with cli_installed():
            built = art.tree_tar(repo.path, sha)
            extracted = art.read_tar(built["payload"])
            self.assertEqual(art.tree_digest((n, d) for n, _, d in extracted), built["tree_digest"])
        self.assertEqual(built["tree_digest"], expected.hexdigest())

    def test_subpath_and_unreadable_commit(self):
        repo, sha = self.repo()
        self.assertEqual([n for n, _, _ in art.tree_files(repo.path, sha, "a")], ["x"])
        self.assertIsNone(art.tree_tar(repo.path, "0" * 40))


class Publish(Case):
    def recipe_registry(self):
        repo = self.sot("| 0.1.2 | tag `recipe-v0.1.2` | Third. |\n")
        repo.commit({"containers/recipe/Dockerfile": "FROM scratch\n", "containers/recipe/build.sh": "#!/bin/sh\n"})
        repo.tag("recipe-v0.1.2")
        repo.publish_head()
        self.sources(sot="o/sot")
        return repo

    def test_manifest_sha256_and_index_artifacts(self):
        self.recipe_registry()
        with cli_installed():
            code, _ = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual(code, 0)
        folder = self.registry / "artifacts/image-recipe/containers__recipe/0.1.2"
        manifest = json.loads((folder / "MANIFEST.json").read_text())
        (artifact,) = manifest["artifacts"]
        self.assertEqual(artifact["name"], "containers__recipe-0.1.2.tar")
        self.assertEqual(artifact["sha256"], art.sha256_file(folder / artifact["name"]))
        self.assertEqual(artifact["bytes"], (folder / artifact["name"]).stat().st_size)
        files = art.read_tar((folder / artifact["name"]).read_bytes())
        self.assertEqual(artifact["tree_digest"], documented_digest((n, d) for n, _, d in files))
        self.assertFalse(artifact["lfs"])
        release = self.index()["components"]["containers/recipe"]["releases"][0]
        self.assertEqual(release["artifacts"], [{k: artifact[k] for k in ("name", "sha256", "bytes", "tree_digest")}])
        self.assertEqual(release["class"], "image-recipe")
        self.assertEqual(self.index()["schema"], "collab.releases/v2")

    def test_republish_is_idempotent_and_a_digest_mismatch_refuses(self):
        self.recipe_registry()
        self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        manifest_file = self.registry / "artifacts/image-recipe/containers__recipe/0.1.2/MANIFEST.json"
        first = manifest_file.read_bytes()
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual((code, manifest_file.read_bytes()), (0, first))
        # a stale recorded digest alone: the stored bytes are these, so the record is repaired
        tampered = json.loads(first)
        tampered["artifacts"][0]["sha256"] = "0" * 64
        manifest_file.write_text(json.dumps(tampered))
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("stale sha256", err)
        self.assertEqual(json.loads(manifest_file.read_text())["artifacts"][0]["sha256"],
                         json.loads(first)["artifacts"][0]["sha256"])
        # stored bytes that differ from the rebuilt ones: an actual mismatch
        tar = manifest_file.parent / "containers__recipe-0.1.2.tar"
        tar.write_bytes(b"other bytes")
        before = (self.registry / "releases.json").read_bytes()
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual(code, 3)
        self.assertIn("other bytes", err)
        self.assertEqual(tar.read_bytes(), b"other bytes")
        self.assertEqual((self.registry / "releases.json").read_bytes(), before)
        # the stored file is gone and the record names other bytes: nothing proves which are right; the record stays
        tar.unlink()
        manifest_file.write_text(json.dumps(tampered))
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("recorded binding is kept", err)
        self.assertEqual(json.loads(manifest_file.read_text())["artifacts"][0]["sha256"], "0" * 64)
        self.assertFalse(tar.exists())

    def test_layer_files_are_copied_and_a_sums_mismatch_refuses(self):
        repo = Repo(self.root, "a-sre", owner="o")
        repo.commit({"CHANGELOG.md": "## 0.1.0\n\nFirst.\n"})
        repo.tag("v0.1.0", "m")
        repo.publish_head()
        self.sources(repositories=["o/a-sre"], classes={"o/a-sre": "layer"})
        files = release_files(self.tmp / "dist", {"collab-a-0.1.0-py3-none-any.whl": b"wheel"},
                              git(repo.path, "rev-parse", "HEAD"), component="o/a-sre")
        digest = art.sha256_bytes(b"wheel")
        (files / "collab-a.SHA256SUMS").write_text(f"{digest}  collab-a-0.1.0-py3-none-any.whl\n")
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "o/a-sre", "v0.1.0", files_dir=files,
                                 requires=["o/cli=>=0.3"], commit_changes=False)
        self.assertEqual(code, 0)
        folder = self.registry / "artifacts/layer/o__a-sre/0.1.0"
        manifest = json.loads((folder / "MANIFEST.json").read_text())
        self.assertEqual({a["name"]: a["lfs"] for a in manifest["artifacts"]},
                         {"collab-a-0.1.0-py3-none-any.whl": True, "collab-a.SHA256SUMS": False,
                          "collab-example.component.json": False})
        self.assertEqual(manifest["requires"], [{"kind": "component", "name": "o/cli", "constraint": ">=0.3"}])
        self.assertEqual(manifest["tag_commit"], git(repo.path, "rev-parse", "HEAD"))
        self.assertEqual((folder / "collab-a-0.1.0-py3-none-any.whl").read_bytes(), b"wheel")
        (files / "collab-a.SHA256SUMS").write_text(f"{'1' * 64}  collab-a-0.1.0-py3-none-any.whl\n")
        shutil_rm = self.registry / "artifacts"
        import shutil
        shutil.rmtree(shutil_rm)
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "o/a-sre", "0.1.0", files_dir=files, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertFalse(shutil_rm.exists())

    def test_php_zip_of_the_tag(self):
        repo = Repo(self.root, "example-core")
        composer = {"name": "example-org/example-core",
                    "require": {"php": "^8.4", "example-org/example-auth": "^1.0", "other/pkg": "^2"}}
        repo.commit({"composer.json": json.dumps(composer), "src/A.php": "<?php\n"})
        repo.tag("v1.2.3", "m")
        repo.publish_head()
        declared = self.declaration({"repositories": ["example-org/example-core"],
                                     "classes": {"example-org/example-core": "php-package"}},
                                    packages={"vendors": ["example-org"]})
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-core", "1.2.3",
                                 commit_changes=False, declaration=declared)
        self.assertEqual(code, 0)
        folder = self.registry / "artifacts/php-package/example-org__example-core/1.2.3"
        with zipfile.ZipFile(folder / "example-core-1.2.3.zip") as z:
            self.assertEqual(sorted(z.namelist()), ["composer.json", "src/", "src/A.php"])
        manifest = json.loads((folder / "MANIFEST.json").read_text())
        self.assertEqual(manifest["package"], "example-org/example-core")
        # the declared vendor filter (packages.vendors) keeps only that vendor's requirements
        self.assertEqual(manifest["requires"], [{"kind": "composer", "name": "example-org/example-auth", "constraint": "^1.0"}])
        self.assertTrue(manifest["artifacts"][0]["lfs"])
        # the same tag gives the same zip
        self.assertEqual(art.php_zip(repo.path, "v1.2.3"), art.php_zip(repo.path, "v1.2.3"))

    def test_unreadable_sources_warn_and_are_skipped_but_the_index_is_written(self):
        repo = Repo(self.root, "present", owner="o")
        repo.commit({"composer.json": "{}"})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        self.sources(repositories=["o/present", "o/absent", "o/notag"], classes={"o/present": "php-package"})
        Repo(self.root, "notag", owner="o").commit({"x": "1"})  # a checkout without origin/HEAD and without tags
        # build: the absent and the unpublished checkouts warn; the present one is indexed
        components, err = self.run_quiet(idx.build, self.root, idx.load_sources(self.registry / "sources.json"), self.registry / "artifacts")
        self.assertEqual(list(components), ["o/present"])
        self.assertIn("o/absent", err)
        self.assertIn("o/notag", err)
        # publish: no tag v9.9.9, no composer in the tag for a missing component: skipped, index still written
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "o/present", "9.9.9", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("no tag v9.9.9", err)
        self.assertFalse((self.registry / "artifacts").exists())
        self.assertIn("o/present", self.index()["components"])
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "o/absent", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("o/absent", err)

    def test_partial_run_keeps_the_previous_entries_of_unreadable_sources(self):
        previous = {"o/gone": {"repository": "o/gone", "kind": "repository", "latest": "1.0.0",
                               "releases": [{"version": "1.0.0", "date": "2026-01-01", "changelog": "x", "commit": None}]}}
        self.sources(repositories=["o/gone"])
        components, err = self.run_quiet(idx.build, self.root, idx.load_sources(self.registry / "sources.json"),
                                         self.registry / "artifacts", previous)
        self.assertIn("kept from the previous index", err)
        release = components["o/gone"]["releases"][0]
        self.assertEqual((release["version"], release["class"], release["artifacts"], release["requires"]), ("1.0.0", "layer", [], []))

    def test_commit_is_left_ready_with_only_the_release(self):
        self.recipe_registry()
        git(self.registry, "init", "-q", "-b", "main")
        (self.registry / "other.txt").write_text("unrelated")
        git(self.registry, "add", "other.txt")
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2")
        self.assertEqual(code, 0)
        self.assertEqual(git(self.registry, "log", "-1", "--format=%s"), "Publicación: containers/recipe 0.1.2")
        changed = git(self.registry, "show", "--name-only", "--format=").splitlines()
        self.assertIn("releases.json", changed)
        self.assertNotIn("other.txt", changed)
        self.assertEqual(git(self.registry, "diff", "--cached", "--name-only"), "other.txt")

    def test_lfs_files_are_not_committed_without_lfs(self):
        self.assertEqual(art.is_lfs("x.whl"), True)
        self.assertEqual(art.is_lfs("x.tar"), False)
        git(self.registry, "init", "-q", "-b", "main")
        (self.registry / "a").mkdir()
        (self.registry / "a/x.zip").write_bytes(b"z")
        reason = pub.lfs_ready(self.registry, ["a/x.zip"])
        self.assertIsNotNone(reason)  # no .gitattributes for this temporary registry, or no git lfs here


def release_files(folder: Path, files: dict[str, bytes], commit: str | None, component: str = "example-org/example-sre",
                  version: str = "0.1.0", list_name: str = "collab-example.component.json") -> Path:
    """A `--files` directory as the runtime's build writes it: the files, and the component release's asset list
    (`collab.component-release/v1`) naming the repository, version and commit, with the sha256 of each file."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    record = {"id": "example", "repository": component, "version": version, "commit": commit}
    listing = {"schema": "collab.component-release/v1", "component": record, "targets": [],
               "files": [{"name": n, "sha256": art.sha256_bytes(d)} for n, d in sorted(files.items())]}
    (folder / list_name).write_text(json.dumps(listing))
    return folder


def layer_repo(case: Case, name: str = "example-sre", tags=("v0.1.0",)) -> Repo:
    repo = Repo(case.root, name)
    repo.commit({"CHANGELOG.md": "".join(f"## {t[1:]}\n\nNotes {t}.\n\n" for t in tags)})
    for tag in tags:
        repo.tag(tag, "m")
    repo.publish_head()
    return repo


class Declaration(Case):
    def test_the_declaration_is_read_and_every_row_records_its_upstream(self):
        repo = layer_repo(self)
        self.sources(repositories=["example-org/other-sre"])  # the former file: not read when a declaration is
        declared = self.declaration({"repositories": ["example-org/example-sre"],
                                     "classes": {"example-org/example-sre": "layer"}})
        sources, err = self.run_quiet(idx.load_sources, self.registry / "sources.json", declared)
        self.assertEqual((sources["origin"], sources["repositories"]), ("declaration", ["example-org/example-sre"]))
        self.assertNotIn("belong in", err)
        components, _ = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts")
        (release,) = components["example-org/example-sre"]["releases"]
        self.assertEqual((release["repository"], release["tag_commit"]),
                         ("example-org/example-sre", git(repo.path, "rev-parse", "v0.1.0^{commit}")))

    def test_the_environment_names_the_declaration(self):
        declared = self.declaration({"repositories": ["example-org/example-sre"]})
        os.environ["COLLAB_RELEASES_DECLARATION"] = str(declared)
        sources, _ = self.run_quiet(idx.load_sources, self.registry / "sources.json")
        self.assertEqual(sources["origin"], "declaration")

    def test_without_a_declaration_sources_json_is_read_and_warns_where_it_belongs(self):
        self.sources(repositories=["example-org/example-sre"])
        sources, err = self.run_quiet(idx.load_sources, self.registry / "sources.json")
        self.assertEqual((sources["origin"], sources["repositories"]), ("sources", ["example-org/example-sre"]))
        self.assertIn("governance/release-adoption.yaml", err)
        self.assertIn("COLLAB_RELEASES_DECLARATION", err)

    def test_an_unreadable_declaration_warns_and_the_former_file_is_read(self):
        self.sources(repositories=["example-org/example-sre"])
        broken = self.tmp / "broken.json"
        broken.write_bytes(b"\xff{ not json")
        sources, err = self.run_quiet(idx.load_sources, self.registry / "sources.json", broken)
        self.assertEqual(sources["repositories"], ["example-org/example-sre"])
        self.assertIn(str(broken), err)

    def test_no_vendor_filter_keeps_every_requirement_with_a_warning(self):
        repo = Repo(self.root, "example-core")
        repo.commit({"composer.json": json.dumps({"name": "example-org/example-core",
                                                  "require": {"php": "^8.4", "other/pkg": "^2"}})})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        self.sources(repositories=["example-org/example-core"], classes={"example-org/example-core": "php-package"})
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-core", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("no Composer vendor filter is declared", err)
        manifest = json.loads((self.registry / "artifacts/php-package/example-org__example-core/1.0.0/MANIFEST.json").read_text())
        self.assertEqual([r["name"] for r in manifest["requires"]], ["other/pkg", "php"])

    def test_bad_declarations_warn_skip_their_source_and_keep_its_previous_entry(self):
        layer_repo(self, "example-sre")
        layer_repo(self, "example-two")
        previous = {"example-org/odd": {"repository": "example-org/odd", "kind": "repository", "class": "layer",
                                        "latest": "1.0.0", "releases": [{"version": "1.0.0", "commit": None}]}}
        declared = self.declaration({"repositories": ["example-org/example-sre", "unqualified", 7, "a/../b",
                                                      "example-org/odd", "example-org/example-two"],
                                     "classes": {"example-org/odd": "not-a-class", "example-org/example-two": "layer"}})
        sources, err = self.run_quiet(idx.load_sources, self.registry / "sources.json", declared)
        self.assertIn("'unqualified'", err)
        self.assertIn("not-a-class", err)
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts", previous)
        self.assertEqual(sorted(components), ["example-org/example-sre", "example-org/example-two", "example-org/odd"])
        self.assertEqual(components["example-org/odd"]["releases"][0]["version"], "1.0.0")
        # classes that are not a mapping: ignored with a warning, every repository reads with the v1 default
        declared = self.declaration({"repositories": ["example-org/example-sre"], "classes": ["layer"]})
        sources, err = self.run_quiet(idx.load_sources, self.registry / "sources.json", declared)
        self.assertIn("`classes` is not a mapping", err)
        self.assertEqual(idx.repository_class(sources, "example-org/example-sre"), "layer")

    def test_nothing_declared_keeps_the_whole_previous_index(self):
        previous = {"example-org/kept": {"repository": "example-org/kept", "kind": "repository", "latest": "1.0.0",
                                         "releases": [{"version": "1.0.0", "commit": None}]}}
        sources, err = self.run_quiet(idx.load_sources, self.registry / "missing-sources.json")
        self.assertFalse(sources["readable"])
        self.assertIn("not found", err)
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts", previous)
        self.assertEqual(list(components), ["example-org/kept"])
        # the publisher: an undeclared component is a successful no-op
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/kept", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("nothing published", err)
        self.assertFalse((self.registry / "releases.json").exists())


class NeutralSchemas(Case):
    def test_new_ids_are_neutral_and_the_former_spelling_is_read(self):
        self.assertEqual((idx.SCHEMA, pub.MANIFEST_SCHEMA), ("collab.releases/v2", "collab.release-manifest/v1"))
        index = self.registry / "releases.json"
        index.write_text(json.dumps({"schema": "example-org.releases/v1", "components": {"example-org/x": {
            "repository": "example-org/x", "kind": "repository", "latest": "1.0.0", "releases": [{"version": "1.0.0"}]}}}))
        (components, state), err = self.run_quiet(idx.load_index, index)
        self.assertEqual((state, list(components)), ("ok", ["example-org/x"]))
        self.assertIn("former spelling", err)
        folder = self.registry / "m"
        folder.mkdir()
        (folder / "MANIFEST.json").write_text(json.dumps({"schema": "example-org.release-manifest/v1", "version": "1.0.0"}))
        manifest, err = self.run_quiet(idx.read_manifest, folder / "MANIFEST.json")
        self.assertEqual(manifest["version"], "1.0.0")
        self.assertIn("former spelling", err)


class ReferenceNotCopy(Case):
    def whole_tree_service(self):
        repo = Repo(self.root, "example-service")
        repo.commit({"app/run.sh": "#!/bin/sh\n", "governance/policy.yaml": "rule: theirs\n",
                     "governance/x/more.yaml": "y\n", "composer.json": "{}", "keep.txt": "k"})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        return repo

    def test_declared_exclusions_never_enter_a_tree_tar_or_a_zip(self):
        self.whole_tree_service()
        declared = self.declaration({"repositories": ["example-org/example-service"],
                                     "classes": {"example-org/example-service": "service"},
                                     "archive_exclude": ["governance"]})
        with cli_installed():
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-service", "1.0.0",
                                       commit_changes=False, declaration=declared)
        self.assertEqual(code, 0)
        folder = self.registry / "artifacts/service/example-org__example-service/1.0.0"
        names = [n for n, _, _ in art.read_tar((folder / "example-org__example-service-1.0.0.tar").read_bytes())]
        self.assertEqual(names, ["app/run.sh", "composer.json", "keep.txt"])
        self.assertNotIn("no archive exclusion", err)
        zipped = art.php_zip(self.root / "example-service", "v1.0.0", ["governance"])
        with zipfile.ZipFile(io.BytesIO(zipped)) as z:
            self.assertFalse([n for n in z.namelist() if n.startswith("governance")])
        self.assertEqual(art.php_zip(self.root / "example-service", "v1.0.0", ["nothing-matches"]),
                         art.php_zip(self.root / "example-service", "v1.0.0"))  # no match: the same bytes as before

    def test_without_an_exclusion_declared_the_archive_warns(self):
        self.whole_tree_service()
        self.sources(repositories=["example-org/example-service"], classes={"example-org/example-service": "service"})
        with cli_installed():
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-service", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("no archive exclusion is declared", err)

    def test_without_the_cli_the_tar_is_published_without_tree_digest(self):
        repo = self.sot("| 0.1.2 | tag `recipe-v0.1.2` | Third. |\n")
        repo.tag("recipe-v0.1.2")
        repo.publish_head()
        self.sources(sot="o/sot")
        with cli_absent():
            self.assertIsNone(art.cli_tree_digest())
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("collab.tree_digest is not installed", err)
        manifest_file = self.registry / "artifacts/image-recipe/containers__recipe/0.1.2/MANIFEST.json"
        (artifact,) = json.loads(manifest_file.read_text())["artifacts"]
        self.assertNotIn("tree_digest", artifact)
        self.assertNotIn("tree_digest", self.index()["components"]["containers/recipe"]["releases"][0]["artifacts"][0])
        # with the cli, the same bytes gain their digest; without it again, the recorded digest is kept
        with cli_installed():
            self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        digest = json.loads(manifest_file.read_text())["artifacts"][0]["tree_digest"]
        with cli_absent():
            code, _ = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual((code, json.loads(manifest_file.read_text())["artifacts"][0]["tree_digest"]), (0, digest))


class FailOpen(Case):
    def test_usage_problems_warn_and_exit_0(self):
        self.sources()
        for argv in ([], ["only-a-component"], ["--files"], ["example-org/x", "1.0.0", "--registry", str(self.registry),
                                                            "--class", "bogus", "--unknown-flag", "--no-commit"]):
            code, err = self.run_quiet(pub.main, argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("warning:", err)
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/x", "../up", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertFalse((self.registry / "artifacts").exists())

    def test_malformed_manifests_warn_and_their_siblings_continue(self):
        layer_repo(self, "example-sre", tags=("v0.1.0", "v0.2.0"))
        self.sources(repositories=["example-org/example-sre"])
        base = self.registry / "artifacts/layer/example-org__example-sre"
        (base / "0.1.0").mkdir(parents=True)
        (base / "0.1.0/MANIFEST.json").write_text(json.dumps({"version": "0.1.0", "artifacts": None, "requires": None}))
        (base / "0.2.0").mkdir()
        (base / "0.2.0/MANIFEST.json").write_text(json.dumps(
            {"version": "0.2.0", "artifacts": [{"name": 5}, {"name": "../x"}, {"name": "ok.whl", "sha256": "a" * 64, "bytes": 1}]}))
        (base / "nightly").mkdir()
        (base / "nightly/MANIFEST.json").write_text(json.dumps({"version": "nightly", "artifacts": []}))
        components, err = self.run_quiet(idx.build, self.root, idx.load_sources(self.registry / "sources.json"), self.registry / "artifacts")
        entry = components["example-org/example-sre"]
        self.assertEqual([r["version"] for r in entry["releases"]], ["0.2.0", "0.1.0", "nightly"])
        self.assertEqual(entry["latest"], "0.2.0")
        by = {r["version"]: r for r in entry["releases"]}
        self.assertEqual(by["0.1.0"]["artifacts"], [])
        self.assertEqual([a["name"] for a in by["0.2.0"]["artifacts"]], ["ok.whl"])
        self.assertIn("`artifacts` is not a list", err)
        self.assertIn("not numeric", err)
        # the publisher reads the same malformed prior manifest and still publishes the version
        files = release_files(self.tmp / "dist", {"new.whl": b"w"},
                              git(self.root / "example-sre", "rev-parse", "v0.2.0^{commit}"), version="0.2.0")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.2.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        names = [a["name"] for a in json.loads((base / "0.2.0/MANIFEST.json").read_text())["artifacts"]]
        self.assertEqual(names, ["collab-example.component.json", "new.whl", "ok.whl"])

    def test_a_malformed_cached_entry_is_kept_as_it_was(self):
        layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        index = self.registry / "releases.json"
        odd = {"repository": "example-org/odd", "kind": "repository", "releases": None}
        index.write_text(json.dumps({"schema": "collab.releases/v2", "components": {"example-org/odd": odd}}))
        files = release_files(self.tmp / "dist", {"a.whl": b"a"}, git(self.root / "example-sre", "rev-parse", "HEAD"))
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertEqual(self.index()["components"]["example-org/odd"], odd)
        self.assertIn("example-org/example-sre", self.index()["components"])
        # the index builder: a declared source with no checkout keeps its malformed entry untouched too
        self.sources(repositories=["example-org/example-sre", "example-org/odd"])
        components, err = self.run_quiet(idx.build, self.root, idx.load_sources(self.registry / "sources.json"),
                                         self.registry / "artifacts", {"example-org/odd": odd})
        self.assertEqual(components["example-org/odd"], odd)
        self.assertIn("malformed; kept as it was", err)
        self.assertIn("example-org/example-sre", components)

    def test_sums_evidence_warns_and_only_an_actual_mismatch_refuses(self):
        repo = layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        tag = git(repo.path, "rev-parse", "HEAD")
        files = release_files(self.tmp / "dist", {"a.whl": b"a"}, tag)
        (files / "SHA256SUMS").write_text("not-a-sum  a.whl\njust-one-field\n")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("is not a SHA-256", err)
        folder = self.registry / "artifacts/layer/example-org__example-sre/0.1.0"
        manifest = json.loads((folder / "MANIFEST.json").read_text())
        # a prior row without sha256, stored bytes equal: warns, publishes
        for row in manifest["artifacts"]:
            row.pop("sha256")
        (folder / "MANIFEST.json").write_text(json.dumps(manifest))
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("no usable sha256", err)
        # a prior row without sha256 whose stored bytes differ: an actual mismatch
        (folder / "MANIFEST.json").write_text(json.dumps(manifest))
        release_files(files, {"a.whl": b"other bytes"}, tag)
        (files / "SHA256SUMS").write_text("")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertEqual((folder / "a.whl").read_bytes(), b"a")

    def test_unreadable_inputs_warn_and_are_skipped(self):
        repo = Repo(self.root, "example-service")
        repo.commit({"a.txt": "a", "b.txt": "b"})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        blob = git(repo.path, "rev-parse", "HEAD:b.txt")
        (repo.path / ".git/objects" / blob[:2] / blob[2:]).unlink()
        self.assertIsNone(art.tree_files(repo.path, "v1.0.0"))
        self.sources(repositories=["example-org/example-service"], classes={"example-org/example-service": "service"})
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-service", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("cannot read the tree", err)
        # an unreadable --files entry is skipped and its siblings are published
        layer = layer_repo(self, "example-sre")
        self.sources(repositories=["example-org/example-sre"])
        files = release_files(self.tmp / "dist", {"good.whl": b"g", "bad.whl": b"b"}, git(layer.path, "rev-parse", "HEAD"))
        real = art.sha256_file

        def failing(path):
            if Path(path).name == "bad.whl":
                raise PermissionError("unreadable")
            return real(path)
        with mock.patch.object(art, "sha256_file", failing):
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                       files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("bad.whl: cannot be read", err)
        self.assertTrue((self.registry / "artifacts/layer/example-org__example-sre/0.1.0/good.whl").exists())
        # a changelog that is not UTF-8 is read with replacement characters
        layer.commit({"CHANGELOG.md": b"## 0.1.0\n\ncaf\xe9\n"})
        layer.publish_head()
        entry = idx.repository_component("example-org/example-sre", layer.path, "layer")
        self.assertIn("caf", entry["releases"][0]["changelog"])

    def test_staged_bytes_are_verified_before_live_files_change(self):
        repo = layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        files = release_files(self.tmp / "dist", {"a.whl": b"a"}, git(repo.path, "rev-parse", "HEAD"))
        (files / "MANIFEST.json").write_text('{"forged": true}')
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("reserves for itself", err)
        folder = self.registry / "artifacts/layer/example-org__example-sre/0.1.0"
        manifest_before = (folder / "MANIFEST.json").read_bytes()
        self.assertNotIn(b"forged", manifest_before)
        index_before = (self.registry / "releases.json").read_bytes()
        real = art.sha256_file
        with mock.patch.object(art, "sha256_file", lambda path: "0" * 64 if Path(path).name.endswith(".part") else real(path)):
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                       files_dir=files, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertEqual((folder / "a.whl").read_bytes(), b"a")
        self.assertEqual((folder / "MANIFEST.json").read_bytes(), manifest_before)
        self.assertEqual((self.registry / "releases.json").read_bytes(), index_before)
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["MANIFEST.json", "a.whl", "collab-example.component.json"])  # nothing staged is left
        # a failure on a first publication leaves no folder behind
        with mock.patch.object(art, "sha256_file", lambda path: "0" * 64 if Path(path).name.endswith(".part") else real(path)):
            code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                     cls="runtime", files_dir=files, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertFalse((self.registry / "artifacts/runtime").exists())

    def test_a_partial_checkout_keeps_every_published_version(self):
        layer_repo(self, tags=("v0.2.0",))  # the checkout has only the newest tag
        self.sources(repositories=["example-org/example-sre"])
        older = {"version": "0.1.0", "date": "2026-01-01", "changelog": "Old.", "commit": None, "class": "layer"}
        (self.registry / "releases.json").write_text(json.dumps({"schema": "collab.releases/v2", "components": {
            "example-org/example-sre": {"repository": "example-org/example-sre", "kind": "repository", "class": "layer",
                                        "latest": "0.1.0", "releases": [older]}}}))
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.2.0", commit_changes=False)
        self.assertEqual(code, 0)
        entry = self.index()["components"]["example-org/example-sre"]
        self.assertEqual(([r["version"] for r in entry["releases"]], entry["latest"]), (["0.2.0", "0.1.0"], "0.2.0"))
        self.assertIn("kept from the previous index", err)
        components, _ = self.run_quiet(idx.build, self.root, idx.load_sources(self.registry / "sources.json"),
                                       self.registry / "artifacts", idx.read_index(self.registry / "releases.json"))
        self.assertEqual([r["version"] for r in components["example-org/example-sre"]["releases"]], ["0.2.0", "0.1.0"])

    def test_an_unreadable_index_is_never_lost(self):
        layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        index = self.registry / "releases.json"
        index.write_text("{ truncated")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertEqual(index.read_text(), "{ truncated")
        self.assertIn("not rewritten", err)
        code, err = self.run_quiet(idx.main, ["--root", str(self.root), "--sources", str(self.registry / "sources.json"),
                                              "--artifacts", str(self.registry / "artifacts"), "--out", str(index)])
        self.assertEqual(code, 0)
        self.assertEqual((self.registry / "releases.json.unreadable").read_text(), "{ truncated")
        self.assertIn("example-org/example-sre", self.index()["components"])



FAKE_LFS_CLEAN = """import hashlib, sys
data = sys.stdin.buffer.read()
sys.stdout.write("version https://git-lfs.github.com/spec/v1\\noid sha256:%s\\nsize %d\\n" % (hashlib.sha256(data).hexdigest(), len(data)))
"""


class SecondReview(Case):
    """Each scenario of the second review, reproduced first (each failed on de31513)."""

    # blocker: --files bound to the requested release
    def test_files_of_another_release_are_refused_and_unbound_files_are_not_advertised(self):
        repo = layer_repo(self, tags=("v1.0.0",))
        old = git(repo.path, "rev-parse", "v1.0.0^{commit}")
        repo.commit({"x": "2"})
        repo.tag("v2.0.0", "m")
        repo.publish_head()
        self.sources(repositories=["example-org/example-sre"])
        wheel = {"example_sre-1.0.0-py3-none-any.whl": b"wheel 1.0.0"}
        files = release_files(self.tmp / "dist-1.0.0", wheel, old, version="1.0.0")
        (files / "SHA256SUMS").write_text(f"{art.sha256_bytes(b'wheel 1.0.0')}  example_sre-1.0.0-py3-none-any.whl\n")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "2.0.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertIn("another release's", err)
        self.assertFalse((self.registry / "artifacts").exists())
        # an asset list of another repository is another release's too
        other = release_files(self.tmp / "dist-other", wheel, git(repo.path, "rev-parse", "v2.0.0^{commit}"),
                              component="example-org/example-other", version="2.0.0")
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "2.0.0",
                                 files_dir=other, commit_changes=False)
        self.assertEqual(code, 3)
        # no asset list at all: nothing advertised, the index is still updated, exit 0
        loose = self.tmp / "loose"
        loose.mkdir()
        (loose / "example_sre-2.0.0-py3-none-any.whl").write_bytes(b"wheel 2.0.0")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "2.0.0",
                                   files_dir=loose, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("holds no asset list of this release", err)
        self.assertFalse((self.registry / "artifacts").exists())
        by = {r["version"]: r for r in self.index()["components"]["example-org/example-sre"]["releases"]}
        self.assertEqual(by["2.0.0"]["artifacts"], [])
        # the release's own asset list binds its files; a file it does not list is not published
        new = git(repo.path, "rev-parse", "v2.0.0^{commit}")
        files = release_files(self.tmp / "dist-2.0.0", {"example_sre-2.0.0-py3-none-any.whl": b"wheel 2.0.0"}, new,
                              version="2.0.0")
        (files / "stray.txt").write_text("not released")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "2.0.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("stray.txt: not in the asset list", err)
        names = [a["name"] for a in self.index()["components"]["example-org/example-sre"]["releases"][0]["artifacts"]]
        self.assertEqual(names, ["collab-example.component.json", "example_sre-2.0.0-py3-none-any.whl"])
        # a listed file with other bytes than the asset list records is the wrong artifact
        (files / "example_sre-2.0.0-py3-none-any.whl").write_bytes(b"tampered")
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "2.0.0",
                                 files_dir=files, commit_changes=False)
        self.assertEqual(code, 3)

    # blocker: cached artifacts stay with the commit they were published from
    def test_a_moved_tag_keeps_the_published_binding(self):
        repo = Repo(self.root, "example-core")
        first = repo.commit({"composer.json": json.dumps({"name": "example-org/example-core"})})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        self.sources(repositories=["example-org/example-core"], classes={"example-org/example-core": "php-package"})
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-core", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        published = self.index()["components"]["example-org/example-core"]["releases"][0]
        moved = repo.commit({"composer.json": json.dumps({"name": "example-org/example-core", "x": 1})})
        git(repo.path, "tag", "-f", "-a", "v1.0.0", "-m", "moved")
        repo.publish_head()
        sources = idx.load_sources(self.registry / "sources.json")
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts", self.index()["components"])
        (release,) = components["example-org/example-core"]["releases"]
        self.assertIn("the published binding is kept", err)
        self.assertEqual((release["commit"], release["tag_commit"]), (first, first))
        self.assertNotEqual(release["commit"], moved)
        self.assertEqual(release["artifacts"], published["artifacts"])
        # publishing the moved tag again: other bytes for a published version are refused, nothing changes
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-core", "1.0.0", commit_changes=False)
        self.assertEqual(code, 3)
        # a manifest in a folder of another version or component is never attached
        folder = self.registry / "artifacts/php-package/example-org__example-core/1.0.0"
        manifest = json.loads((folder / "MANIFEST.json").read_text())
        manifest["version"] = "9.9.9"
        (folder / "MANIFEST.json").write_text(json.dumps(manifest))
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts")
        self.assertEqual(components["example-org/example-core"]["releases"][0]["artifacts"], [])
        self.assertIn("not of 1.0.0", err)

    # blocker: a checkout is resolved by owner and name, and its identity checked
    def test_a_checkout_of_another_owner_is_never_read(self):
        Repo(self.root, "core", owner="example-a").commit({"composer.json": "{}"})
        git(self.root / "core", "tag", "-a", "v1.0.0", "-m", "m")
        Repo.publish_head(types.SimpleNamespace(path=self.root / "core"))
        self.sources(repositories=["example-b/core"], classes={"example-b/core": "php-package"})
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-b/core", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("is of example-a/core, not of example-b/core", err)
        self.assertFalse((self.registry / "artifacts").exists())
        # the owner's own folder, <root>/<owner>/<name>, is read
        mine = Repo(self.root, "core", owner="example-b", folder=self.root / "example-b" / "core")
        mine.commit({"composer.json": json.dumps({"name": "example-b/core"})})
        mine.tag("v1.0.0", "m")
        mine.publish_head()
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-b/core", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        manifest = json.loads((self.registry / "artifacts/php-package/example-b__core/1.0.0/MANIFEST.json").read_text())
        self.assertEqual(manifest["tag_commit"], git(mine.path, "rev-parse", "HEAD"))
        # an origin with credentials in its URL: only owner/name is ever used, never printed
        git(mine.path, "remote", "set-url", "origin", "https://user:EXAMPLE-NOT-A-SECRET@example.test/example-c/core.git")
        _, err = self.run_quiet(idx.checkout, self.root, "example-b/core")
        self.assertIn("example-c/core", err)
        self.assertNotIn("EXAMPLE-NOT-A-SECRET", err)

    # major: a --class override with --path is its own component, both found in the index
    def test_a_service_of_a_php_package_repository_is_its_own_component(self):
        repo = Repo(self.root, "example-core")
        repo.commit({"composer.json": json.dumps({"name": "example-org/example-core"}), "service/run.sh": "#!/bin/sh\n"})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        self.sources(repositories=["example-org/example-core"], classes={"example-org/example-core": "php-package"})
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-core", "1.0.0", commit_changes=False)
        self.assertEqual(code, 0)
        with cli_installed():
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-core", "1.0.0",
                                       cls="service", tree_path="service", commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("published as its own component example-org/example-core/service", err)
        for components in (self.index()["components"],
                           self.run_quiet(idx.build, self.root, idx.load_sources(self.registry / "sources.json"),
                                          self.registry / "artifacts", self.index()["components"])[0]):
            php, service = components["example-org/example-core"], components["example-org/example-core/service"]
            self.assertEqual((php["class"], [a["name"] for a in php["releases"][0]["artifacts"]]),
                             ("php-package", ["example-core-1.0.0.zip"]))
            self.assertEqual((service["class"], service["repository"], service["kind"]),
                             ("service", "example-org/example-core", "repository"))
            self.assertEqual([a["name"] for a in service["releases"][0]["artifacts"]],
                             ["example-org__example-core__service-1.0.0.tar"])
            self.assertEqual(service["releases"][0]["tag_commit"], git(repo.path, "rev-parse", "HEAD"))
        # a --class override without --path is found under the class it was published as
        layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        files = release_files(self.tmp / "dist", {"a.whl": b"a"}, git(self.root / "example-sre", "rev-parse", "HEAD"))
        self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0", cls="runtime",
                       files_dir=files, commit_changes=False)
        release = self.index()["components"]["example-org/example-sre"]["releases"][0]
        self.assertEqual((release["class"], [a["name"] for a in release["artifacts"]]),
                         ("runtime", ["a.whl", "collab-example.component.json"]))

    # major: only the release is staged, and LFS artifacts are verified as pointers
    def lfs_registry(self, clean: str | None):
        git(self.registry, "init", "-q", "-b", "main")
        git(self.registry, "config", "commit.gpgsign", "false")
        if clean is not None:
            (self.registry / ".gitattributes").write_text("artifacts/**/*.whl filter=lfs diff=lfs merge=lfs -text\n")
            git(self.registry, "config", "filter.lfs.clean", clean)
            git(self.registry, "config", "filter.lfs.required", "true")

    def test_a_retry_stages_only_the_release_and_never_a_plain_binary(self):
        repo = layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        self.lfs_registry(None)  # no LFS attribute: the first run leaves the wheel uncommitted
        files = release_files(self.tmp / "dist", {"a.whl": b"wheel"}, git(repo.path, "rev-parse", "HEAD"))
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0", files_dir=files)
        self.assertEqual(code, 0)
        self.assertIn("not committed", err)
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0")
        self.assertEqual(code, 0)
        self.assertIn("not committed", err)
        self.assertEqual(git(self.registry, "diff", "--cached", "--name-only"), "")
        self.assertEqual(subprocess.run(["git", "-C", str(self.registry), "rev-parse", "-q", "--verify", "HEAD"],
                                        capture_output=True).returncode, 1)

    def test_lfs_artifacts_are_committed_only_as_pointers_to_their_bytes(self):
        repo = layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        files = release_files(self.tmp / "dist", {"a.whl": b"wheel"}, git(repo.path, "rev-parse", "HEAD"))
        script = self.tmp / "fake-lfs-clean.py"
        script.write_text(FAKE_LFS_CLEAN)
        # a clean filter that stores the bytes as they are is not Git LFS: nothing is staged
        self.lfs_registry("cat")
        with mock.patch.object(pub, "lfs_ready", return_value=None):
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0", files_dir=files)
        self.assertEqual(code, 0)
        self.assertIn("ordinary blob", err)
        self.assertEqual(git(self.registry, "diff", "--cached", "--name-only"), "")
        # an (obviously fake) clean filter that writes the LFS pointer of the bytes: committed, with the stray file left out
        git(self.registry, "config", "filter.lfs.clean", f"{sys.executable} -I {script}")
        folder = self.registry / "artifacts/layer/example-org__example-sre/0.1.0"
        (folder / "left-by-a-failed-run.bin").write_bytes(b"x")
        with mock.patch.object(pub, "lfs_ready", return_value=None):
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0", files_dir=files)
        self.assertEqual(code, 0)
        committed = git(self.registry, "show", "--name-only", "--format=").splitlines()
        self.assertIn("artifacts/layer/example-org__example-sre/0.1.0/a.whl", committed)
        self.assertNotIn("artifacts/layer/example-org__example-sre/0.1.0/left-by-a-failed-run.bin", committed)
        pointer = git(self.registry, "cat-file", "blob", "HEAD:artifacts/layer/example-org__example-sre/0.1.0/a.whl")
        self.assertIn(f"oid sha256:{art.sha256_bytes(b'wheel')}", pointer)

    # major: a failure while replacing leaves the release as it was
    def test_a_failed_replacement_rolls_back(self):
        repo = layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        files = release_files(self.tmp / "dist", {"a.whl": b"a", "b.whl": b"b"}, git(repo.path, "rev-parse", "HEAD"))
        folder = self.registry / "artifacts/layer/example-org__example-sre/0.1.0"
        (folder / "b.whl").mkdir(parents=True)  # the second wheel's destination is a directory
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("is not a regular file", err)
        self.assertEqual([p.name for p in folder.iterdir()], ["b.whl"])  # nothing moved in, nothing staged left
        (folder / "b.whl").rmdir()
        self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0", files_dir=files,
                       commit_changes=False)
        before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in folder.iterdir()}
        # a rename that fails half way: every replaced live file is put back (the very same files)
        real, calls = os.replace, []

        def failing(src, dst):
            calls.append(Path(dst).name)
            if Path(dst).name == "b.whl" and Path(src).name.endswith(".part"):
                raise OSError("disk full")
            return real(src, dst)
        with mock.patch.object(pub.os, "replace", failing):
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.1.0",
                                       files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertIn("could not be updated", err)
        self.assertIn("a.whl", calls)  # the first one was replaced before the failure
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_ino) for p in folder.iterdir()}, before)

    # major: an unreadable declaration never lets an excluded path into an archive
    def test_an_unreadable_declaration_uses_its_verified_copy_or_builds_no_archive(self):
        repo = Repo(self.root, "example-service")
        repo.commit({"app/run.sh": "#!/bin/sh\n", "governance/policy.yaml": "rule: theirs\n"})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        self.sources(repositories=["example-org/example-service"], classes={"example-org/example-service": "service"})
        git(self.registry, "init", "-q", "-b", "main")
        yaml_declaration = self.tmp / "release-adoption.yaml"
        yaml_declaration.write_text("publication:\n  repositories: [example-org/example-service]\n"
                                    "  classes: {example-org/example-service: service}\n  archive_exclude: [governance]\n")
        folder = self.registry / "artifacts/service/example-org__example-service/1.0.0"
        with mock.patch.dict(sys.modules, {"yaml": None}), cli_installed():  # a host without PyYAML
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-service", "1.0.0",
                                       commit_changes=False, declaration=yaml_declaration)
            self.assertEqual(code, 0)
            self.assertIn("no archive is built", err)
            self.assertFalse(folder.exists())
            self.assertIn("example-org/example-service", self.index()["components"])
            # once read (as its JSON rendering), the verified copy is kept and used when the file cannot be read
            readable = self.declaration({"repositories": ["example-org/example-service"],
                                         "classes": {"example-org/example-service": "service"},
                                         "archive_exclude": ["governance"]})
            sources, _ = self.run_quiet(idx.load_sources, self.registry / "sources.json", readable, pub.decl.cache_file(self.registry))
            self.assertEqual((sources["origin"], sources["exclude"]), ("declaration", ["governance"]))
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-service", "1.0.0",
                                       commit_changes=False, declaration=yaml_declaration)
        self.assertEqual(code, 0)
        self.assertIn("last verified copy", err)
        names = [n for n, _, _ in art.read_tar((folder / "example-org__example-service-1.0.0.tar").read_bytes())]
        self.assertEqual(names, ["app/run.sh"])
        # bytes that do not match the provenance the canon reader wrote are not used either
        Path(str(readable) + ".provenance.json").write_text(json.dumps({"sha256": "0" * 64}))
        sources, err = self.run_quiet(idx.load_sources, self.registry / "sources.json", readable, pub.decl.cache_file(self.registry))
        self.assertIn("do not match the sha256", err)
        self.assertEqual((sources["origin"], sources["exclude"]), ("cache", ["governance"]))

    # major: a component that cannot be read keeps its history
    def test_an_unreadable_version_json_keeps_the_component(self):
        repo = self.sot("| 0.1.2 | tag `recipe-v0.1.2` | Third. |\n")
        repo.tag("recipe-v0.1.2")
        repo.publish_head()
        self.sources(sot="o/sot")
        sources = idx.load_sources(self.registry / "sources.json")
        previous, _ = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts")
        repo.commit({"containers/recipe/version.json": "{ not json"})
        repo.publish_head()
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts", previous)
        self.assertIn("version.json is not JSON", err)
        self.assertEqual(components["containers/recipe"], previous["containers/recipe"])

    # major: a partial checkout keeps the commit resolved before
    def test_an_unresolvable_tag_keeps_the_resolved_commit(self):
        repo = self.sot("| 0.1.2 | tag `recipe-v0.1.2` | Third. |\n")
        repo.tag("recipe-v0.1.2")
        repo.publish_head()
        self.sources(sot="o/sot")
        sources = idx.load_sources(self.registry / "sources.json")
        previous, _ = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts")
        resolved = previous["containers/recipe"]["releases"][0]
        self.assertIsNotNone(resolved["commit"])
        git(repo.path, "tag", "-d", "recipe-v0.1.2")  # a fetch without that tag
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts", previous)
        release = components["containers/recipe"]["releases"][0]
        self.assertEqual((release["commit"], release["tag_commit"]), (resolved["commit"], resolved["tag_commit"]))
        self.assertIn("previously resolved", err)


class ThirdReview(Case):
    """Each finding of the third review (head 99bb02a), reproduced first: each of these failed on 99bb02a."""

    def asset_list_dir(self, name: str, files: dict[str, bytes], record: dict, **top) -> Path:
        """A `--files` directory whose asset list carries exactly `record` (a field a test omits stays omitted)."""
        folder = self.tmp / name
        folder.mkdir(parents=True)
        for file_name, data in files.items():
            (folder / file_name).write_bytes(data)
        listing = {"schema": "collab.component-release/v1", "component": record, "targets": [],
                   "files": [{"name": n, "sha256": art.sha256_bytes(d)} for n, d in sorted(files.items())], **top}
        (folder / "collab-example.component.json").write_text(json.dumps(listing))
        return folder

    # blocker: a matching commit is not a matching release
    def test_two_tags_on_one_commit_do_not_accept_each_others_assets(self):
        repo = layer_repo(self, tags=("v1.0.0", "v1.0.1"))  # one commit, two tags
        self.sources(repositories=["example-org/example-sre"])
        commit = git(repo.path, "rev-parse", "HEAD")
        files = release_files(self.tmp / "dist-1.0.0", {"example_sre-1.0.0-py3-none-any.whl": b"wheel 1.0.0"}, commit,
                              version="1.0.0")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "1.0.1",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertIn("another release's", err)
        self.assertFalse((self.registry / "artifacts").exists())
        # the release the list names is published
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "1.0.0",
                                 files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertTrue((self.registry / "artifacts/layer/example-org__example-sre/1.0.0/MANIFEST.json").is_file())
        self.assertFalse((self.registry / "artifacts/layer/example-org__example-sre/1.0.1").exists())

    def test_a_list_that_names_two_versions_binds_nothing_it_has_not_agreed_on(self):
        repo = layer_repo(self, tags=("v1.0.0", "v1.0.1"))
        self.sources(repositories=["example-org/example-sre"])
        commit = git(repo.path, "rev-parse", "HEAD")
        record = {"repository": "example-org/example-sre", "version": "1.0.1", "commit": commit}
        files = self.asset_list_dir("dist-mixed", {"a.whl": b"w"}, record, version="1.0.0")
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "1.0.1",
                                 files_dir=files, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertFalse((self.registry / "artifacts").exists())

    # blocker: a version alone, with no repository and no commit, binds nothing
    def test_an_asset_list_without_repository_or_commit_binds_nothing(self):
        repo = layer_repo(self, tags=("v1.0.0",))
        self.sources(repositories=["example-org/example-sre"])
        commit = git(repo.path, "rev-parse", "HEAD")
        for label, record in (("bare", {"version": "1.0.0"}),
                              ("no-repository", {"version": "1.0.0", "commit": commit}),
                              ("no-commit", {"repository": "example-org/example-sre", "version": "1.0.0"}),
                              ("no-version", {"repository": "example-org/example-sre", "commit": commit})):
            files = self.asset_list_dir(f"dist-{label}", {"a.whl": b"w"}, record)
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "1.0.0",
                                       files_dir=files, commit_changes=False)
            self.assertEqual(code, 0, label)
            self.assertIn("binds nothing", err, label)
            self.assertFalse((self.registry / "artifacts").exists(), label)
            by = {r["version"]: r for r in self.index()["components"]["example-org/example-sre"]["releases"]}
            self.assertEqual(by["1.0.0"]["artifacts"], [], label)
        # the whole record binds
        files = self.asset_list_dir("dist-whole", {"a.whl": b"w"},
                                    {"repository": "example-org/example-sre", "version": "1.0.0", "commit": commit})
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "1.0.0",
                                 files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        self.assertTrue((self.registry / "artifacts/layer/example-org__example-sre/1.0.0/a.whl").is_file())

    # blocker: --commit cannot attach other bytes to a tagged release
    def test_a_commit_override_cannot_replace_the_commit_of_a_tagged_sot_release(self):
        repo = self.sot("| 0.1.2 | tag `recipe-v0.1.2` | Third. |\n")
        tagged = repo.commit({"containers/recipe/Dockerfile": "FROM scratch\n"})
        repo.tag("recipe-v0.1.2")
        other = repo.commit({"containers/recipe/Dockerfile": "FROM scratch\nRUN other\n"})
        repo.publish_head()
        self.sources(sot="o/sot")
        with cli_installed():
            code, err = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2",
                                       commit=other, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertIn("tag", err)
        self.assertFalse((self.registry / "artifacts").exists())
        self.assertFalse((self.registry / "releases.json").exists())
        # the commit the tag names (even abbreviated) is the same release: published, and the index says so
        with cli_installed():
            code, _ = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2",
                                     commit=tagged[:12], commit_changes=False)
        self.assertEqual(code, 0)
        manifest = json.loads((self.registry / "artifacts/image-recipe/containers__recipe/0.1.2/MANIFEST.json").read_text())
        self.assertEqual((manifest["commit"], manifest["tag_commit"]), (tagged, tagged))
        release = self.index()["components"]["containers/recipe"]["releases"][0]
        self.assertEqual((release["commit"], release["tag_commit"]), (tagged, tagged))

    def test_a_commit_override_cannot_replace_the_commit_of_a_tagged_repository_tree(self):
        repo = Repo(self.root, "example-service")
        tagged = repo.commit({"app/run.sh": "#!/bin/sh\n"})
        repo.tag("v1.0.0", "m")
        other = repo.commit({"app/run.sh": "#!/bin/sh\necho other\n"})
        repo.publish_head()
        self.sources(repositories=["example-org/example-service"], classes={"example-org/example-service": "service"})
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-service", "1.0.0",
                                   commit=other, commit_changes=False)
        self.assertEqual(code, 3)
        self.assertFalse((self.registry / "artifacts").exists())
        # without a tag the override is the release's commit, as the README says
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-service", "1.0.1",
                                 commit=other, commit_changes=False)
        self.assertEqual(code, 0)
        manifest = json.loads((self.registry / "artifacts/service/example-org__example-service/1.0.1/MANIFEST.json").read_text())
        self.assertEqual(manifest["commit"], other)
        self.assertIsNone(manifest.get("tag_commit"))

    def test_a_manifest_whose_commit_and_tag_commit_differ_is_not_taken_for_agreeing(self):
        """What the override bug already published: the tag's commit matched, the bytes' commit did not."""
        repo = Repo(self.root, "example-service")
        tagged = repo.commit({"app/run.sh": "#!/bin/sh\n"})
        repo.tag("v1.0.0", "m")
        other = repo.commit({"app/run.sh": "#!/bin/sh\necho other\n"})
        repo.publish_head()
        self.sources(repositories=["example-org/example-service"], classes={"example-org/example-service": "service"})
        folder = self.registry / "artifacts/service/example-org__example-service/1.0.0"
        folder.mkdir(parents=True)
        (folder / "MANIFEST.json").write_text(json.dumps({
            "schema": "collab.release-manifest/v1", "component": "example-org/example-service", "class": "service",
            "version": "1.0.0", "repository": "example-org/example-service", "tag": "v1.0.0", "tag_commit": tagged,
            "commit": other, "artifacts": [{"name": "s.tar", "sha256": "a" * 64, "bytes": 1}]}))
        sources = idx.load_sources(self.registry / "sources.json")
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts")
        (release,) = components["example-org/example-service"]["releases"]
        self.assertEqual(release["commit"], other)  # the commit of the bytes, not the tag's
        self.assertIn("the published binding is kept", err)

    # major: an unreadable manifest after a moved tag keeps the commit the cached bytes came from
    def test_a_moved_tag_with_an_unreadable_manifest_keeps_the_commit_of_the_cached_artifacts(self):
        repo = Repo(self.root, "example-core")
        first = repo.commit({"composer.json": json.dumps({"name": "example-org/example-core"})})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        self.sources(repositories=["example-org/example-core"], classes={"example-org/example-core": "php-package"})
        self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-core", "1.0.0", commit_changes=False)
        published = self.index()["components"]["example-org/example-core"]["releases"][0]
        self.assertTrue(published["artifacts"])
        repo.commit({"composer.json": json.dumps({"name": "example-org/example-core", "x": 1})})
        git(repo.path, "tag", "-f", "-a", "v1.0.0", "-m", "moved")
        repo.publish_head()
        manifest = self.registry / "artifacts/php-package/example-org__example-core/1.0.0/MANIFEST.json"
        manifest.write_text("{ not json")
        sources = idx.load_sources(self.registry / "sources.json")
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts", self.index()["components"])
        (release,) = components["example-org/example-core"]["releases"]
        self.assertEqual(release["artifacts"], published["artifacts"])
        self.assertEqual((release["commit"], release["tag_commit"]), (first, first))
        self.assertIn("the published binding is kept", err)

    # major: a declaration that cannot be read never shrinks the index to what sources.json lists
    def test_an_unreadable_declaration_without_a_copy_keeps_the_repositories_only_the_canon_declared(self):
        layer_repo(self, "example-sre")
        layer_repo(self, "example-canon-only")
        self.sources(repositories=["example-org/example-sre"])
        declared = self.declaration({"repositories": ["example-org/example-sre", "example-org/example-canon-only"]})
        sources, _ = self.run_quiet(idx.load_sources, self.registry / "sources.json", declared, pub.decl.cache_file(self.registry))
        previous, _ = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts")
        self.assertEqual(sorted(previous), ["example-org/example-canon-only", "example-org/example-sre"])
        declared.write_text("{ not json")  # the canon file cannot be read now, and no verified copy is kept
        sources, err = self.run_quiet(idx.load_sources, self.registry / "sources.json", declared, self.tmp / "no-copy.json")
        self.assertIn("no verified copy", err)
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts", previous)
        self.assertEqual(sorted(components), ["example-org/example-canon-only", "example-org/example-sre"])
        self.assertIn("example-org/example-canon-only: kept from the previous index", err)
        # a readable declaration is the authority: a repository it no longer lists leaves the index
        declared = self.declaration({"repositories": ["example-org/example-sre"]})
        sources, _ = self.run_quiet(idx.load_sources, self.registry / "sources.json", declared, self.tmp / "no-copy.json")
        components, _ = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts", previous)
        self.assertEqual(sorted(components), ["example-org/example-sre"])

    # major: history kept from a v1 index is read as v2
    def test_versions_kept_from_a_v1_index_gain_the_v2_fields(self):
        layer_repo(self, tags=("v0.3.0",))  # the checkout has only the newest tag
        self.sources(repositories=["example-org/example-sre"])
        v1_row = {"version": "0.2.1", "date": "2026-01-01", "changelog": "Old.", "commit": "abc1234def"}
        (self.registry / "releases.json").write_text(json.dumps({"schema": "collab.releases/v1", "components": {
            "example-org/example-sre": {"repository": "example-org/example-sre", "kind": "repository", "latest": "0.2.1",
                                        "releases": [v1_row]}}}))
        sources = idx.load_sources(self.registry / "sources.json")
        components, err = self.run_quiet(idx.build, self.root, sources, self.registry / "artifacts",
                                         idx.read_index(self.registry / "releases.json"))
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.3.0", commit_changes=False)
        self.assertEqual(code, 0)
        for rows in ([r for r in components["example-org/example-sre"]["releases"]],
                     self.index()["components"]["example-org/example-sre"]["releases"]):
            self.assertEqual([r["version"] for r in rows], ["0.3.0", "0.2.1"])
            for row in rows:
                for field in ("class", "repository", "tag_commit", "artifacts", "requires"):
                    self.assertIn(field, row, (row["version"], field))
            kept = rows[1]
            self.assertEqual((kept["class"], kept["repository"], kept["artifacts"], kept["requires"], kept["commit"]),
                             ("layer", "example-org/example-sre", [], [], "abc1234def"))


if __name__ == "__main__":
    unittest.main()
