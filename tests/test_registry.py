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

    def __init__(self, root: Path, name: str):
        self.path = root / name
        self.path.mkdir(parents=True)
        git(self.path, "init", "-q", "-b", "main")
        git(self.path, "config", "commit.gpgsign", "false")
        git(self.path, "config", "tag.gpgsign", "false")

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
        repo = Repo(self.root, "sot")
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
        tampered = json.loads(first)
        tampered["artifacts"][0]["sha256"] = "0" * 64
        manifest_file.write_text(json.dumps(tampered))
        before = (self.registry / "releases.json").read_bytes()
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual(code, 3)
        self.assertIn("another sha256", err)
        self.assertEqual((self.registry / "releases.json").read_bytes(), before)

    def test_layer_files_are_copied_and_a_sums_mismatch_refuses(self):
        repo = Repo(self.root, "a-sre")
        repo.commit({"CHANGELOG.md": "## 0.1.0\n\nFirst.\n"})
        repo.tag("v0.1.0", "m")
        repo.publish_head()
        self.sources(repositories=["o/a-sre"], classes={"o/a-sre": "layer"})
        files = self.tmp / "dist"
        files.mkdir()
        (files / "collab-a-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
        digest = art.sha256_bytes(b"wheel")
        (files / "collab-a.SHA256SUMS").write_text(f"{digest}  collab-a-0.1.0-py3-none-any.whl\n")
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "o/a-sre", "v0.1.0", files_dir=files,
                                 requires=["o/cli=>=0.3"], commit_changes=False)
        self.assertEqual(code, 0)
        folder = self.registry / "artifacts/layer/o__a-sre/0.1.0"
        manifest = json.loads((folder / "MANIFEST.json").read_text())
        self.assertEqual({a["name"]: a["lfs"] for a in manifest["artifacts"]},
                         {"collab-a-0.1.0-py3-none-any.whl": True, "collab-a.SHA256SUMS": False})
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
        repo = Repo(self.root, "present")
        repo.commit({"composer.json": "{}"})
        repo.tag("v1.0.0", "m")
        repo.publish_head()
        self.sources(repositories=["o/present", "o/absent", "o/notag"], classes={"o/present": "php-package"})
        Repo(self.root, "notag").commit({"x": "1"})  # a checkout without origin/HEAD and without tags
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
        self.sources(sot="example-org/example-sot")
        (self.root / "example-sot").symlink_to(repo.path)
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
        files = self.tmp / "dist"
        files.mkdir()
        (files / "new.whl").write_bytes(b"w")
        code, err = self.run_quiet(pub.publish, self.registry, self.root, "example-org/example-sre", "0.2.0",
                                   files_dir=files, commit_changes=False)
        self.assertEqual(code, 0)
        names = [a["name"] for a in json.loads((base / "0.2.0/MANIFEST.json").read_text())["artifacts"]]
        self.assertEqual(names, ["new.whl", "ok.whl"])

    def test_a_malformed_cached_entry_is_kept_as_it_was(self):
        layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        index = self.registry / "releases.json"
        odd = {"repository": "example-org/odd", "kind": "repository", "releases": None}
        index.write_text(json.dumps({"schema": "collab.releases/v2", "components": {"example-org/odd": odd}}))
        files = self.tmp / "dist"
        files.mkdir()
        (files / "a.whl").write_bytes(b"a")
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
        layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        files = self.tmp / "dist"
        files.mkdir()
        (files / "a.whl").write_bytes(b"a")
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
        (files / "a.whl").write_bytes(b"other bytes")
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
        files = self.tmp / "dist"
        files.mkdir()
        (files / "good.whl").write_bytes(b"g")
        (files / "bad.whl").write_bytes(b"b")
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
        layer_repo(self)
        self.sources(repositories=["example-org/example-sre"])
        files = self.tmp / "dist"
        files.mkdir()
        (files / "a.whl").write_bytes(b"a")
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
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["MANIFEST.json", "a.whl"])  # nothing staged is left
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


if __name__ == "__main__":
    unittest.main()
