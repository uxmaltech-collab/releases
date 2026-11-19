"""Tests of the index builder and the publisher, on temporary git repositories. Standard library only.

    python3 -I -m unittest
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import artifacts as art  # noqa: E402
import build_index as idx  # noqa: E402
import publish as pub  # noqa: E402

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
        self.old_env = {k: os.environ.get(k) for k in IDENTITY}
        os.environ.update(IDENTITY)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def sources(self, **kw):
        data = {"schema": "uxmaltech-collab.release-sources/v2", "sot": None, "repositories": [], "classes": {}}
        data.update(kw)
        (self.registry / "sources.json").write_text(json.dumps(data))

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
        repo = Repo(self.root, "agents-sre")
        repo.commit({"CHANGELOG.md": "# c\n\n## 0.2.0\n\nSecond.\n\n## 0.1.0\n\nFirst.\n"}, date="2026-03-01T00:00:00Z")
        repo.tag("v0.1.0", "first")
        sha = repo.commit({"x": "1"}, date="2026-04-01T00:00:00Z")
        repo.tag("v0.2.0", "second", date="2026-04-01T00:00:00Z")
        repo.publish_head()
        self.sources(repositories=["uxmaltech-collab/agents-sre"])
        components = idx.build(self.root, idx.load_sources(self.registry / "sources.json"), self.registry / "artifacts")
        idx.write_index(self.registry / "releases.json", components)
        data = self.index()
        self.assertEqual(data["schema"], "uxmaltech-collab.releases/v2")
        entry = data["components"]["uxmaltech-collab/agents-sre"]
        # What a v1 reader (PublicReleases::read) uses.
        self.assertEqual((entry["repository"], entry["kind"], entry["latest"]), ("uxmaltech-collab/agents-sre", "repository", "0.2.0"))
        newest, oldest = entry["releases"]
        self.assertEqual((newest["version"], newest["date"], newest["changelog"]), ("0.2.0", "2026-04-01", "Second."))
        self.assertEqual((oldest["version"], oldest["changelog"]), ("0.1.0", "First."))
        self.assertEqual(newest["commit"], sha)
        # What v2 adds.
        self.assertEqual((newest["class"], newest["tag_commit"], newest["artifacts"], newest["requires"]), ("layer", sha, [], []))

    def test_v1_sources_file_still_reads_as_layers(self):
        (self.registry / "sources.json").write_text(json.dumps(
            {"schema": "uxmaltech-collab.release-sources/v1", "sot": "o/sot", "repositories": ["o/a-sre"]}))
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
        built = art.tree_tar(repo.path, sha)
        self.assertEqual(built["tree_digest"], expected.hexdigest())
        extracted = art.read_tar(built["payload"])
        self.assertEqual(art.tree_digest((n, d) for n, _, d in extracted), built["tree_digest"])

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
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "containers/recipe", "0.1.2", commit_changes=False)
        self.assertEqual(code, 0)
        folder = self.registry / "artifacts/image-recipe/containers__recipe/0.1.2"
        manifest = json.loads((folder / "MANIFEST.json").read_text())
        (artifact,) = manifest["artifacts"]
        self.assertEqual(artifact["name"], "containers__recipe-0.1.2.tar")
        self.assertEqual(artifact["sha256"], art.sha256_file(folder / artifact["name"]))
        self.assertEqual(artifact["bytes"], (folder / artifact["name"]).stat().st_size)
        files = art.read_tar((folder / artifact["name"]).read_bytes())
        self.assertEqual(artifact["tree_digest"], art.tree_digest((n, d) for n, _, d in files))
        self.assertFalse(artifact["lfs"])
        release = self.index()["components"]["containers/recipe"]["releases"][0]
        self.assertEqual(release["artifacts"], [{k: artifact[k] for k in ("name", "sha256", "bytes", "tree_digest")}])
        self.assertEqual(release["class"], "image-recipe")
        self.assertEqual(self.index()["schema"], "uxmaltech-collab.releases/v2")

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
        repo = Repo(self.root, "core")
        composer = {"name": "uxmaltech/core", "require": {"php": "^8.4", "uxmaltech/auth-client": "^1.0", "other/pkg": "^2"}}
        repo.commit({"composer.json": json.dumps(composer), "src/A.php": "<?php\n"})
        repo.tag("v1.2.3", "m")
        repo.publish_head()
        self.sources(repositories=["uxmaltech/core"], classes={"uxmaltech/core": "php-package"})
        code, _ = self.run_quiet(pub.publish, self.registry, self.root, "uxmaltech/core", "1.2.3", commit_changes=False)
        self.assertEqual(code, 0)
        folder = self.registry / "artifacts/php-package/uxmaltech__core/1.2.3"
        with zipfile.ZipFile(folder / "core-1.2.3.zip") as z:
            self.assertEqual(sorted(z.namelist()), ["composer.json", "src/", "src/A.php"])
        manifest = json.loads((folder / "MANIFEST.json").read_text())
        self.assertEqual(manifest["package"], "uxmaltech/core")
        self.assertEqual(manifest["requires"], [{"kind": "composer", "name": "uxmaltech/auth-client", "constraint": "^1.0"}])
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


if __name__ == "__main__":
    unittest.main()
