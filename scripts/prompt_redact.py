#!/usr/bin/env python3
"""Reversible local prompt redaction for cloud LLM calls.

This is not an isolation boundary. It reduces what leaves the machine and what
lands in logs. Keep the mapping file in a private directory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


class RedactionError(ValueError):
    pass


def _chmod_private(path: Path) -> None:
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def load_map(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {"version": 1, "terms": {}}
    try:
        data = json.loads(file_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RedactionError(f"cannot read mapping file: {exc}") from exc
    if not isinstance(data, dict):
        raise RedactionError("mapping file must contain a JSON object")
    terms = data.get("terms")
    if not isinstance(terms, dict):
        terms = {}
    clean: dict[str, str] = {}
    for token, value in terms.items():
        if isinstance(token, str) and isinstance(value, str) and token and value:
            clean[token] = value
    data["version"] = 1
    data["terms"] = clean
    return data


def save_map(path: str | Path, data: dict[str, Any]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    file_path.write_text(payload, encoding="utf-8")
    _chmod_private(file_path)


def _next_token(terms: dict[str, str]) -> str:
    highest = 0
    for token in terms:
        match = re.fullmatch(r"__REDACTED_(\d+)__", token)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"__REDACTED_{highest + 1:04d}__"


def add_term(path: str | Path, value: str) -> str:
    value = value.strip()
    if not value:
        raise RedactionError("term must not be empty")
    data = load_map(path)
    terms: dict[str, str] = data["terms"]
    for token, existing in terms.items():
        if existing == value:
            return token
    token = _next_token(terms)
    while token in terms:
        token = _next_token(terms)
    terms[token] = value
    save_map(path, data)
    return token


def redact(text: str, data: dict[str, Any], ignore_case: bool = False) -> str:
    pairs = [(value, token) for token, value in data.get("terms", {}).items() if value]
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    result = text
    for value, token in pairs:
        if ignore_case:
            result = re.sub(re.escape(value), token, result, flags=re.IGNORECASE)
        else:
            result = result.replace(value, token)
    return result


def restore(text: str, data: dict[str, Any]) -> str:
    result = text
    for token, value in data.get("terms", {}).items():
        result = result.replace(token, value)
    return result


def _mask(value: str) -> str:
    if len(value) <= 2:
        return "*" * len(value)
    return value[0] + "*" * max(1, len(value) - 2) + value[-1]


def _read_input(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    return sys.stdin.read()


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Local reversible prompt redaction.")
    sub = parser.add_subparsers(dest="command", required=True)

    add_parser = sub.add_parser("add", help="Add a sensitive term and print its token.")
    add_parser.add_argument("--map", required=True, dest="map_path")
    add_parser.add_argument("--term", required=True)

    list_parser = sub.add_parser("list", help="List tokens (values masked by default).")
    list_parser.add_argument("--map", required=True, dest="map_path")
    list_parser.add_argument("--show", action="store_true", help="Show plaintext values.")

    for name in ("redact", "restore"):
        command = sub.add_parser(name, help=f"{name.capitalize()} text.")
        command.add_argument("--map", required=True, dest="map_path")
        command.add_argument("--text")
        command.add_argument("--file")
        if name == "redact":
            command.add_argument("--ignore-case", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "add":
            token = add_term(args.map_path, args.term)
            print(token)
            return 0
        if args.command == "list":
            data = load_map(args.map_path)
            terms = data.get("terms", {})
            if not terms:
                print("(empty)")
                return 0
            for token in sorted(terms):
                value = terms[token] if args.show else _mask(terms[token])
                print(f"{token}\t{value}")
            return 0
        data = load_map(args.map_path)
        text = _read_input(args)
        if args.command == "redact":
            print(redact(text, data, ignore_case=getattr(args, "ignore_case", False)), end="")
        else:
            print(restore(text, data), end="")
        return 0
    except (OSError, RedactionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
