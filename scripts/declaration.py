"""Generic reader of the publisher's declaration: which repositories it publishes, their classes, the Composer
vendors its requirements are filtered by and the paths no archive may carry.

The declaration is the organization's, not this repository's: it lives in the organization canon as
`governance/release-adoption.yaml` (schema `collab.release-adoption-declaration/v1`) and the publisher is given
its path with `--declaration` or `COLLAB_RELEASES_DECLARATION` (no default). This reader knows the shape, never
the content:

    publication:
      sot: <owner>/<name>                      # optional: the source-of-truth repository whose components it publishes
      repositories: [<owner>/<name>, ...]      # repositories with releases of their own
      classes: {<component key>: <class>}      # class of each one (none: the v1 default)
      archive_exclude: [<path or glob>, ...]   # repository-relative paths never packed in a tar or a zip
    packages:
      vendors: [<composer vendor>, ...]        # the Composer requirements a release records (none: all of them)

Every key is optional. A file that cannot be read, an unknown key or a wrong-typed value warns and is ignored;
the rest is read. The file is JSON (the canon reader's `release-adoption.v1.json` rendering); a YAML file is read
when PyYAML is installed. Standard library only otherwise.

The verified cache (EDR-008 rule 3): every time the declaration is read and its `publication` is well formed, its
bytes' sha256 and what was read are kept in the registry checkout's git directory
(`<git dir>/collab-releases/declaration.verified.json`: never committed, never published). When the declaration
given cannot be read (or its `publication` is malformed), the last verified copy is used, with a warning that says
its sha256 and age. When a `<file>.provenance.json` (`{sha256, ...}`, written by the canon reader) is beside the
declaration, bytes that do not match its sha256 are not used.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SCHEMA = "collab.release-adoption-declaration/v1"
ENV = "COLLAB_RELEASES_DECLARATION"
CANON_PATH = "governance/release-adoption.yaml"
WHERE = (f"the organization canon's {CANON_PATH} (`publication`, `packages.vendors`), given with --declaration "
         f"or {ENV}")
PUBLICATION_KEYS = {"sot", "repositories", "classes", "archive_exclude"}


WARNINGS: list[str] = []  # every warning of this run, so a command can say how many it gave


def warn(text: str) -> None:
    """A warning line on stderr (paths and labels only: nothing secret is ever passed here)."""
    WARNINGS.append(text)
    print(f"warning: {text}", file=sys.stderr)


class Parser(argparse.ArgumentParser):
    """An argument parser whose usage errors warn instead of exiting 2: a bad flag is not a reason to stop."""

    def error(self, message: str):  # noqa: D401 - argparse's hook
        raise UsageWarning(message)


class UsageWarning(Exception):
    pass


def path_from(given: str | Path | None) -> Path | None:
    """The declaration's path: the flag, else the environment, else none."""
    value = given if given else os.environ.get(ENV)
    return Path(value) if value else None


def _parse(path: Path, text: str):
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        import yaml  # optional: only when the machine has it
    except ImportError:
        warn(f"{path}: not JSON and PyYAML is not installed; give the JSON rendering of the declaration")
        return None
    try:
        return yaml.safe_load(text)
    except Exception as error:  # a YAML error of any kind is a warning
        warn(f"{path}: not readable as YAML ({type(error).__name__}); declaration ignored")
        return None


def read(path: Path) -> dict | None:
    """The declaration as a dict, or None (with a warning) when it cannot be read."""
    return read_with_digest(path)[0]


def read_with_digest(path: Path) -> tuple[dict | None, str | None]:
    """`(declaration, sha256 of its bytes)`, or `(None, None)` with a warning when it cannot be read, or when a
    provenance file beside it records another sha256 for it."""
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, ValueError) as error:
        warn(f"{path}: the declaration cannot be read ({type(error).__name__}); it is ignored")
        return None, None
    digest = hashlib.sha256(raw).hexdigest()
    recorded = _provenance_sha256(path)
    if recorded is not None and recorded != digest:
        warn(f"{path}: its bytes do not match the sha256 its provenance file records; it is not used")
        return None, None
    data = _parse(path, text)
    if data is None:
        return None, None
    if not isinstance(data, dict):
        warn(f"{path}: the declaration is not a mapping; it is ignored")
        return None, None
    if data.get("schema") not in (None, SCHEMA):
        warn(f"{path}: unknown declaration schema {data.get('schema')!r}; reading the keys it shares with {SCHEMA}")
    return data, digest


def _provenance_sha256(path: Path) -> str | None:
    beside = path.with_name(path.name + ".provenance.json")
    if not beside.exists():
        return None
    try:
        value = json.loads(beside.read_text(encoding="utf-8")).get("sha256")
    except (OSError, ValueError, AttributeError):
        warn(f"{beside}: the provenance file cannot be read; the declaration's bytes are not checked against it")
        return None
    if isinstance(value, str) and len(value) == 64:
        return value.lower()
    warn(f"{beside}: records no sha256; the declaration's bytes are not checked against it")
    return None


def cache_file(registry: Path) -> Path | None:
    """Where the last verified copy of the declaration is kept: inside the registry checkout's git directory, so it
    is never committed nor published. None when the registry is not a git checkout (there is then no cache)."""
    done = subprocess.run(["git", "-C", str(registry), "rev-parse", "--absolute-git-dir"], capture_output=True, text=True)
    if done.returncode != 0 or not done.stdout.strip():
        return None
    return Path(done.stdout.strip()) / "collab-releases" / "declaration.verified.json"


def remember(cache: Path | None, path: Path, digest: str, data: dict) -> None:
    """Keep `data` as the last verified copy (its source path, sha256 and read time). A cache that cannot be written
    warns; nothing else changes."""
    if cache is None:
        return
    record = {"schema": "collab.release-declaration-cache/v1", "path": str(path), "sha256": digest,
              "read_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
              "data": data}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        part = cache.with_name(f".{cache.name}.part")
        part.write_text(json.dumps(record, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        os.replace(part, cache)
    except OSError as error:
        warn(f"{cache}: the verified copy of the declaration cannot be kept ({type(error).__name__})")


def recall(cache: Path | None) -> dict | None:
    """The last verified copy `{path, sha256, read_at, data}`, or None."""
    if cache is None or not cache.exists():
        return None
    try:
        record = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        warn(f"{cache}: the verified copy of the declaration cannot be read ({type(error).__name__}); not used")
        return None
    if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
        warn(f"{cache}: the verified copy of the declaration is not usable; not used")
        return None
    return record


def age(read_at) -> str:
    try:
        then = dt.datetime.fromisoformat(str(read_at).replace("Z", "+00:00"))
    except ValueError:
        return "of unknown age"
    hours = (dt.datetime.now(dt.timezone.utc) - then).total_seconds() / 3600
    return f"{hours:.0f} h old" if hours < 48 else f"{hours / 24:.0f} days old"


def exclusions(publication_value) -> list[str] | None:
    """The `archive_exclude` of a `publication` value: a list (empty when none is declared), or None when it is
    malformed (then which paths the organization excludes is not known)."""
    if publication_value is None:
        return []
    if not isinstance(publication_value, dict):
        return None
    value = publication_value.get("archive_exclude")
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    return string_list("publication.archive_exclude", value)


def string_list(where: str, value) -> list[str] | None:
    """A list of non-empty strings, or None (with a warning) when it is something else; bad items warn."""
    if value is None:
        return None
    if not isinstance(value, list):
        warn(f"{where}: not a list; ignored")
        return None
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        else:
            warn(f"{where}: {item!r} is not a non-empty string; ignored")
    return out


def publication(data: dict, path: Path) -> dict | None:
    """The `publication` mapping of a declaration, or None when it declares none."""
    value = data.get("publication")
    if value is None:
        return None
    if not isinstance(value, dict):
        warn(f"{path}: `publication` is not a mapping; ignored")
        return None
    for unknown in sorted(set(value) - PUBLICATION_KEYS, key=str):
        warn(f"{path}: unknown key publication.{unknown}; ignored")
    return value


def vendors(data: dict | None, path: Path | None) -> list[str] | None:
    """`packages.vendors`, lower case, or None when not declared."""
    if not data:
        return None
    packages = data.get("packages")
    if packages is None:
        return None
    if not isinstance(packages, dict):
        warn(f"{path}: `packages` is not a mapping; ignored")
        return None
    found = string_list(f"{path}: packages.vendors", packages.get("vendors"))
    return [v.lower() for v in found] if found is not None else None
