from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import datetime as dt
from pathlib import Path
from typing import Any, Optional


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n\n[TRUNCATED]\n"


def normalize_model_markdown(raw: str, fallback_title: str) -> str:
    cleaned = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    lines = cleaned.splitlines()
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            return "\n".join(lines[idx:]).strip()
    body = cleaned.strip()
    return f"{fallback_title}\n\n{body}" if body else fallback_title


def sanitize_plan_commands(plan_md: str, allowed_commands: list[str]) -> str:
    if not allowed_commands:
        return plan_md
    replacement = "## Commands to validate\n" + "\n".join(f"- `{cmd}`" for cmd in allowed_commands[:4])
    pattern = re.compile(r"## Commands to validate\s*\n(?:.*\n?)*?\Z", re.MULTILINE)
    if pattern.search(plan_md):
        return pattern.sub(replacement, plan_md).rstrip() + "\n"
    return plan_md.rstrip() + "\n\n" + replacement + "\n"


def safe_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def require_nonempty(value: str, name: str, hint: str = "") -> None:
    if not str(value or "").strip():
        extra = f"\nSugerencia: {hint}" if hint else ""
        raise SystemExit(f"Falta valor requerido: {name}.{extra}")


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    return f"{seconds:.2f}s"


ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return strip_ansi(value.decode("utf-8", errors="replace"))
    return strip_ansi(str(value))


class ExecutionFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        command: str = "",
        returncode: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
        timeout_seconds: Optional[int] = None,
        status: str = "failed",
    ) -> None:
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timeout_seconds = timeout_seconds
        self.status = status

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "command": self.command,
            "returncode": self.returncode,
            "timeout_seconds": self.timeout_seconds,
            "stdout": truncate(self.stdout, 12000),
            "stderr": truncate(self.stderr, 12000),
            "status": self.status,
        }


def run_cmd(
    cmd: str | list[str],
    cwd: Path | None = None,
    env: dict | None = None,
    check: bool = False,
    capture: bool = True,
    timeout: Optional[int] = None,
    use_shell: bool = False,
    input_text: Optional[str] = None,
) -> tuple[int, str, str]:
    if isinstance(cmd, str):
        args = cmd if use_shell else shlex.split(cmd)
    else:
        if use_shell:
            raise ValueError("use_shell=True no es compatible con cmd como lista.")
        args = cmd

    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        text=True,
        input=input_text,
        capture_output=capture,
        check=False,
        timeout=timeout,
        shell=use_shell,
    )
    if check and proc.returncode != 0:
        shown = args if isinstance(args, str) else " ".join(shlex.quote(x) for x in args)
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {shown}\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )
    return proc.returncode, proc.stdout, proc.stderr


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def ensure_ai_dir(repo: Path, ai_dir_name: str) -> Path:
    ai_dir = repo / (ai_dir_name or "AI")
    ai_dir.mkdir(parents=True, exist_ok=True)
    return ai_dir


def resolve_goals_path(repo: Path, goals_value: str) -> Path:
    goals_value = (goals_value or "").strip() or "PROJECT_GOALS.md"
    gp = Path(goals_value).expanduser()
    if gp.is_absolute():
        return gp
    return repo / gp


def looks_like_shell_command(cmd: str) -> bool:
    shell_markers = ["&&", "||", "|", ";", ">", "<", "$(", "`"]
    return any(marker in cmd for marker in shell_markers)


def repo_tree(repo: Path, max_lines: int = 600) -> str:
    code, out, err = run_cmd("git ls-files", cwd=repo, capture=True)
    if code != 0:
        return f"[Could not list files]\n{err}"

    files = out.strip().splitlines()
    skip_patterns = [
        r"^AI/.*",
        r"^\.git/.*",
        r"^node_modules/.*",
        r"^dist/.*",
        r"^build/.*",
        r"^\.venv/.*",
        r"^venv/.*",
        r"^__pycache__/.*",
        r"^\.mypy_cache/.*",
        r"^\.pytest_cache/.*",
        r"^\.ruff_cache/.*",
    ]
    rx = re.compile("|".join(f"(?:{p})" for p in skip_patterns))
    files = [f for f in files if not rx.match(f)]
    files = files[:max_lines]
    return "\n".join(files)


def head_file(repo: Path, rel: str, max_chars: int = 8000) -> str:
    p = repo / rel
    if not p.exists() or not p.is_file():
        return f"[Missing file: {rel}]"
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"[Could not read {rel}: {exc}]"
    return truncate(txt, max_chars)
