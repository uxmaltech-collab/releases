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
"""
from __future__ import annotations

import argparse
import json
import os
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
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        warn(f"{path}: the declaration cannot be read ({type(error).__name__}); it is ignored")
        return None
    data = _parse(path, text)
    if data is None:
        return None
    if not isinstance(data, dict):
        warn(f"{path}: the declaration is not a mapping; it is ignored")
        return None
    if data.get("schema") not in (None, SCHEMA):
        warn(f"{path}: unknown declaration schema {data.get('schema')!r}; reading the keys it shares with {SCHEMA}")
    return data


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
