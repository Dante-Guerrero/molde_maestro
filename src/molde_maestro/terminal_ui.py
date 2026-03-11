from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping, Sequence


_COLOR_BY_KIND = {
    "info": "\033[36m",
    "ok": "\033[32m",
    "warn": "\033[33m",
    "error": "\033[31m",
}
_LABEL_BY_KIND = {
    "info": "INFO",
    "ok": "OK",
    "warn": "WARNING",
    "error": "ERROR",
}
_RESET = "\033[0m"


def _supports_color(stream) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)()) and not bool(getattr(sys, "platform", "").startswith("win"))


def _styled_label(kind: str, stream=None) -> str:
    stream = stream or sys.stdout
    label = _LABEL_BY_KIND.get(kind, kind.upper())
    if not _supports_color(stream):
        return f"[{label}]"
    color = _COLOR_BY_KIND.get(kind, "")
    return f"{color}[{label}]{_RESET}"


def print_section(title: str) -> None:
    print(f"\n== {title} ==")


def print_status(kind: str, message: str, *, stream=None) -> None:
    stream = stream or sys.stdout
    print(f"{_styled_label(kind, stream)} {message}", file=stream)


def print_list(title: str, items: Sequence[str], *, limit: int = 10) -> None:
    print(title)
    shown = list(items[:limit])
    for item in shown:
        print(f"- {item}")
    if len(items) > limit:
        print(f"- ... y {len(items) - limit} más")


def print_kv_summary(title: str, data: Mapping[str, object]) -> None:
    print(title)
    for key, value in data.items():
        if value in (None, "", [], {}):
            continue
        print(f"- {key}: {value}")


def confirm_action(prompt: str, *, default_no: bool = True) -> bool:
    suffix = "[y/N]" if default_no else "[Y/n]"
    answer = input(f"{prompt} {suffix} ").strip().lower()
    if not answer:
        return not default_no
    return answer in {"y", "yes", "s", "si"}


def print_artifact_hint(label: str, path: Path | str) -> None:
    print_status("info", f"{label}: {path}")


def print_human_error_summary(stage: str, message: str, error_path: Path | str, *, hint: str = "") -> None:
    print_status("error", f"{stage}: {message}")
    if hint:
        print_status("info", hint)
    print_artifact_hint("Detalle", error_path)
