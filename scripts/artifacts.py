"""Byte-level helpers of the release registry: hashes, the tree digest, the deterministic tree tar, the PHP zip.

The tree tar and its `tree_digest` follow the contract the console stores (`collab.sot-package/v1`) and the
cli's `collab.sot_package` writes: the tree is read through git (`ls-tree`, `cat-file`), never `git archive`;
USTAR (no pax headers), regular files only, entries in tree-digest order (path COMPONENT by component),
uid and gid 0 with empty names, the commit's committer time as every mtime, mode 0644 or 0755. The tree digest
is the sha256 over, per file in that order, the UTF-8 relative path, a NUL byte and the raw 32-byte sha256 of
the file's bytes. Files under a `__pycache__` directory are not packed and not counted. The same commit and
path give the same bytes.

Standard library only.
"""
from __future__ import annotations

import hashlib
import io
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Iterable

CACHE = "__pycache__"
REGULAR = {b"100644": False, b"100755": True}
# What goes through Git LFS (keep in step with .gitattributes): zips of PHP packages and wheels. Tree tars and
# the small text files of a composition (manifest, sums, install.py) stay in plain git.
LFS_SUFFIXES = (".zip", ".whl", ".tar.gz", ".tgz")
_DROPPED = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE")


class Refusal(Exception):
    """A sha256 or tree digest that does not match: the only thing the publisher refuses over."""


def git_bytes(repo: Path, *args: str, data: bytes | None = None) -> bytes | None:
    """Raw stdout of a git command in `repo`, or None when git fails. Replacement refs and prompts are off."""
    env = {k: v for k, v in os.environ.items() if k not in _DROPPED}
    env.update({"LC_ALL": "C", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0"})
    done = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, env=env)
    return done.stdout if done.returncode == 0 else None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def is_lfs(name: str) -> bool:
    return name.endswith(LFS_SUFFIXES)


def sort_key(relative: str) -> list[str]:
    """Path order: component by component, never by the joined text (`a/b` sorts before `a.txt`)."""
    return relative.split("/")


def tree_digest(entries: Iterable[tuple[str, bytes]]) -> str:
    """The digest of `(relative path, bytes)` pairs (the cli's `collab.tree_digest`)."""
    digest = hashlib.sha256()
    for relative, content in sorted(entries, key=lambda entry: sort_key(entry[0])):
        if CACHE in relative.split("/"):
            continue
        digest.update(relative.encode() + b"\0")
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def tree_files(repo: Path, commit: str, path: str = "") -> list[tuple[str, bool, bytes]] | None:
    """`(relative path, executable, bytes)` of every file of `<commit>:<path>` in digest order, or None when the
    commit or the path cannot be read there. A link or a submodule in the tree is a ValueError."""
    spec = f"{commit}:{path}" if path else f"{commit}^{{tree}}"
    listing = git_bytes(repo, "ls-tree", "-r", "-z", spec)
    if listing is None:
        return None
    found = []
    for row in filter(None, listing.split(b"\0")):
        head, _, name = row.partition(b"\t")
        mode, _, identity = head.split(b" ")
        if mode not in REGULAR:
            raise ValueError("a tree tar holds regular files only: the tree has a link or a submodule")
        relative = name.decode("utf-8", "surrogateescape")
        if CACHE not in relative.split("/"):
            found.append((relative, REGULAR[mode], identity))
    found.sort(key=lambda row: sort_key(row[0]))
    if not found:
        return []
    answer = git_bytes(repo, "cat-file", "--batch", data=b"".join(row[2] + b"\n" for row in found))
    if answer is None:
        return None
    result, offset = [], 0
    for name, executable, _ in found:
        header_end = answer.index(b"\n", offset)
        size = int(answer[offset:header_end].split(b" ")[2])
        start = header_end + 1
        result.append((name, executable, answer[start:start + size]))
        offset = start + size + 1
    return result


def write_tar(files: list[tuple[str, bool, bytes]], mtime: int) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, executable, data in files:
            member = tarfile.TarInfo(name)
            member.size, member.mtime, member.mode = len(data), mtime, 0o755 if executable else 0o644
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            member.type = tarfile.REGTYPE
            tar.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def read_tar(payload: bytes) -> list[tuple[str, bool, bytes]]:
    """The regular files of a tree tar; any other entry, an absolute path or `..` is a ValueError."""
    out = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as tar:
        for member in tar:
            parts = member.name.rstrip("/").split("/")
            if member.name.startswith("/") or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("a tree tar entry is outside the archive's root")
            if member.isdir():
                continue
            if not member.isreg():
                raise ValueError("a tree tar entry is not a regular file")
            stream = tar.extractfile(member)
            out.append(("/".join(parts), bool(member.mode & 0o111), stream.read() if stream else b""))
    return out


def tree_tar(repo: Path, commit: str, path: str = "") -> dict | None:
    """The tree tar of `<commit>:<path>` as `{payload, tree_digest, files, mtime}`, or None when unreadable.

    The tar is read back and its digest compared with the one computed from git: a difference is a Refusal.
    """
    full = (git_bytes(repo, "rev-parse", "--verify", "-q", f"{commit}^{{commit}}") or b"").decode().strip()
    mtime_text = (git_bytes(repo, "show", "-s", "--format=%ct", full) or b"").decode().strip() if full else ""
    if not full or not mtime_text:
        return None
    files = tree_files(repo, full, path)
    if not files:
        return None
    payload = write_tar(files, int(mtime_text))
    digest = tree_digest((name, data) for name, _, data in files)
    if tree_digest((name, data) for name, _, data in read_tar(payload)) != digest:
        raise Refusal("the tree tar read back does not give the tree digest computed from git")
    return {"payload": payload, "tree_digest": digest, "files": len(files), "commit": full}


def php_zip(repo: Path, tag: str) -> bytes | None:
    """`git archive` of the tag as a stored (uncompressed) zip with `composer.json` inside, or None.

    Stored entries keep the bytes independent of the zlib the machine has; the archive carries the commit
    time as every entry's time, so the same tag gives the same zip.
    """
    if git_bytes(repo, "cat-file", "-e", f"refs/tags/{tag}:composer.json") is None:
        return None
    return git_bytes(repo, "archive", "-0", "--format=zip", f"refs/tags/{tag}")
