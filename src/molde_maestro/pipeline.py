#!/usr/bin/env python3
"""
pipeline.py — Repo improvement pipeline: plan (reasoner) -> apply (aider) -> test -> report.

Mejoras principales:
- Soporte explícito para comandos shell en lint/test.
- Preflight de herramientas, modelos, goals y estado del repo.
- Preparación del repo para Aider (evita prompts típicos de .gitignore).
- Lógica de rama base más segura.
- Política configurable de lint: puede reportar o bloquear.
- Mejor logging y mensajes de error.

Config discovery order:
  1) --config <path>
  2) <cwd>/molde_maestro.yml
  3) <cwd>/molde_maestro.yaml
  4) <cwd>/molde_maestro.json

CLI overrides config.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import contextlib
import dataclasses
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import copy
from pathlib import Path
from typing import Optional, Tuple, List, Any, Dict


# -----------------------------
# Utilities
# -----------------------------

def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n\n[TRUNCATED]\n"


def normalize_model_markdown(raw: str, fallback_title: str) -> str:
    """
    Keep the first markdown heading onward and strip common reasoning wrappers.
    This avoids saving model preambles like "Thinking..." or <think> blocks.
    """
    cleaned = re.sub(r"<think>.*?</think>\s*", "", raw, flags=re.DOTALL | re.IGNORECASE).strip()
    lines = cleaned.splitlines()
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            return "\n".join(lines[idx:]).strip()
    body = cleaned.strip()
    return f"{fallback_title}\n\n{body}" if body else fallback_title


def sanitize_plan_commands(plan_md: str, allowed_commands: List[str]) -> str:
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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": str(self),
            "command": self.command,
            "returncode": self.returncode,
            "timeout_seconds": self.timeout_seconds,
            "stdout": truncate(self.stdout, 12000),
            "stderr": truncate(self.stderr, 12000),
            "status": self.status,
        }


@dataclasses.dataclass
class RunRecorder:
    ai_dir: Path
    command: str
    repo: Path
    config_path: Optional[Path] = None
    metadata_name: str = "run-metadata.json"
    metadata_path: Path = dataclasses.field(init=False)
    metadata: Dict[str, Any] = dataclasses.field(init=False)
    _started_perf: Dict[str, float] = dataclasses.field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.metadata_path = self.ai_dir / self.metadata_name
        self.metadata = {
            "command": self.command,
            "repo": str(self.repo),
            "ai_dir": str(self.ai_dir),
            "config_path": str(self.config_path) if self.config_path else None,
            "started_at": iso_now(),
            "completed_at": None,
            "status": "running",
            "stages": [],
            "error": None,
        }
        self.save()

    def save(self) -> None:
        safe_write(self.metadata_path, json.dumps(self.metadata, indent=2, ensure_ascii=False))

    def _stage(self, name: str) -> Dict[str, Any]:
        for stage in self.metadata["stages"]:
            if stage["name"] == name:
                return stage
        stage = {
            "name": name,
            "start_time": None,
            "end_time": None,
            "duration_seconds": None,
            "status": "pending",
            "details": {},
        }
        self.metadata["stages"].append(stage)
        return stage

    def start_stage(self, name: str) -> None:
        stage = self._stage(name)
        stage["start_time"] = iso_now()
        stage["end_time"] = None
        stage["duration_seconds"] = None
        stage["status"] = "running"
        self._started_perf[name] = dt.datetime.now().timestamp()
        self.save()
        print(f"[{name}] started at {stage['start_time']}")

    def finish_stage(self, name: str, status: str, details: Optional[Dict[str, Any]] = None) -> None:
        stage = self._stage(name)
        stage["end_time"] = iso_now()
        start_perf = self._started_perf.pop(name, None)
        duration = None if start_perf is None else max(0.0, dt.datetime.now().timestamp() - start_perf)
        stage["duration_seconds"] = round(duration, 3) if duration is not None else None
        stage["status"] = status
        if details:
            stage["details"].update(details)
        self.save()
        print(f"[{name}] {status} in {format_duration(stage['duration_seconds'])}")

    def mark_stage_skipped(self, name: str, reason: str) -> None:
        stage = self._stage(name)
        stage["status"] = "skipped"
        stage["details"]["reason"] = reason
        if stage["start_time"] is None:
            stage["start_time"] = iso_now()
        stage["end_time"] = stage["start_time"]
        stage["duration_seconds"] = 0.0
        self.save()
        print(f"[{name}] skipped ({reason})")

    def fail_run(self, message: str, status: str = "failed", details: Optional[Dict[str, Any]] = None) -> None:
        self.metadata["status"] = status
        self.metadata["completed_at"] = iso_now()
        self.metadata["error"] = {"message": message, "details": details or {}}
        self.save()

    def complete_run(self, status: str = "ok", details: Optional[Dict[str, Any]] = None) -> None:
        self.metadata["status"] = status
        self.metadata["completed_at"] = iso_now()
        if details:
            self.metadata["summary"] = details
        self.save()


def run_cmd(
    cmd: str | List[str],
    cwd: Path | None = None,
    env: dict | None = None,
    check: bool = False,
    capture: bool = True,
    timeout: Optional[int] = None,
    use_shell: bool = False,
    input_text: Optional[str] = None,
) -> Tuple[int, str, str]:
    """
    Run a command and return (returncode, stdout, stderr).

    - If cmd is str and use_shell=False -> shlex.split(cmd)
    - If cmd is str and use_shell=True  -> run through shell
    - If cmd is list -> run directly
    """
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


def ensure_git_repo(repo: Path) -> None:
    code, out, _ = run_cmd("git rev-parse --is-inside-work-tree", cwd=repo)
    if code != 0 or out.strip() != "true":
        raise SystemExit(f"No es un repo git válido: {repo}")


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
    """
    Lightweight tracked file listing, excluding common junk.
    """
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
    except Exception as e:
        return f"[Could not read {rel}: {e}]"
    return truncate(txt, max_chars)


# -----------------------------
# Config loading (YAML/JSON)
# -----------------------------

def _normalize_cfg_keys(cfg: Any) -> Any:
    if isinstance(cfg, dict):
        out: Dict[str, Any] = {}
        for k, v in cfg.items():
            nk = str(k).strip().replace("-", "_")
            out[nk] = _normalize_cfg_keys(v)
        return out
    if isinstance(cfg, list):
        return [_normalize_cfg_keys(x) for x in cfg]
    return cfg


def load_config_file(config_path: str | None) -> tuple[dict, Optional[Path]]:
    """
    Load config from YAML or JSON.

    Search order:
      1) --config path if provided
      2) <cwd>/molde_maestro.yml
      3) <cwd>/molde_maestro.yaml
      4) <cwd>/molde_maestro.json
    """
    cwd = Path.cwd()
    candidates: List[Path] = []

    if config_path:
        cp = Path(config_path).expanduser()
        if not cp.is_absolute():
            cp = cwd / cp
        candidates.append(cp)
    else:
        candidates += [
            cwd / "molde_maestro.yml",
            cwd / "molde_maestro.yaml",
            cwd / "molde_maestro.json",
        ]

    chosen = next((p for p in candidates if p.exists()), None)
    if not chosen:
        return {}, None

    suffix = chosen.suffix.lower()

    if suffix in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            if config_path:
                raise SystemExit("Hay config YAML pero falta PyYAML. Instala: pip install pyyaml")
            print(
                f"warning: se ignoró {chosen} porque falta PyYAML; continúa con flags/defaults.",
                file=sys.stderr,
            )
            return {}, None
        raw = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
    elif suffix == ".json":
        raw = json.loads(chosen.read_text(encoding="utf-8"))
    else:
        return {}, chosen

    cfg = _normalize_cfg_keys(raw)
    if not isinstance(cfg, dict):
        raise SystemExit("El config debe ser un objeto/dict en YAML o JSON.")
    return cfg, chosen


def init_run_context(args) -> tuple[Path, Path, RunRecorder]:
    repo = Path(args.repo).expanduser().resolve()
    ai_dir = ensure_ai_dir(repo, args.ai_dir)
    cfg_path = getattr(args, "_config_path", None)
    metadata_name = "run-metadata.json"
    if args.cmd == "report" and (ai_dir / "run-metadata.json").exists():
        metadata_name = "report-command-metadata.json"
    recorder = RunRecorder(ai_dir=ai_dir, command=args.cmd, repo=repo, config_path=cfg_path, metadata_name=metadata_name)
    return repo, ai_dir, recorder


def clear_artifacts(ai_dir: Path, names: List[str]) -> None:
    for name in names:
        safe_unlink(ai_dir / name)


PLAN_MODE_PRESETS: Dict[str, Dict[str, int]] = {
    "fast": {
        "max_tree_lines": 120,
        "max_files": 4,
        "max_file_chars": 2500,
        "max_changes": 3,
    },
    "balanced": {
        "max_tree_lines": 300,
        "max_files": 8,
        "max_file_chars": 4500,
        "max_changes": 5,
    },
    "deep": {
        "max_tree_lines": 600,
        "max_files": 12,
        "max_file_chars": 8000,
        "max_changes": 7,
    },
}


def resolve_plan_settings(args, mode_override: Optional[str] = None) -> PlanSettings:
    mode = (mode_override or args.plan_mode or "balanced").strip().lower()
    if mode not in PLAN_MODE_PRESETS:
        raise SystemExit(f"plan_mode inválido: {mode}. Usa fast, balanced o deep.")
    preset = PLAN_MODE_PRESETS[mode]
    return PlanSettings(
        mode=mode,
        max_tree_lines=args.plan_max_tree_lines or preset["max_tree_lines"],
        max_files=args.plan_max_files or preset["max_files"],
        max_file_chars=args.plan_max_file_chars or preset["max_file_chars"],
        max_changes=args.plan_max_changes or preset["max_changes"],
    )


def effective_plan_fallback_timeout(args) -> int:
    return args.plan_fallback_timeout or args.reasoner_timeout


def build_plan_attempt_specs(args) -> List[PlanAttemptSpec]:
    attempts: List[PlanAttemptSpec] = [
        PlanAttemptSpec(
            index=1,
            mode=args.plan_mode,
            reasoner=args.reasoner,
            timeout=args.reasoner_timeout,
            label="primary",
        )
    ]
    next_index = 2
    seen = {(args.plan_mode, args.reasoner)}

    if args.plan_retry_on_timeout:
        degraded = ("fast", args.reasoner)
        if degraded not in seen:
            attempts.append(
                PlanAttemptSpec(
                    index=next_index,
                    mode="fast",
                    reasoner=args.reasoner,
                    timeout=args.reasoner_timeout,
                    label="retry-fast",
                )
            )
            seen.add(degraded)
            next_index += 1

        if args.plan_fallback_reasoner.strip():
            fallback = ("fast", args.plan_fallback_reasoner.strip())
            if fallback not in seen:
                attempts.append(
                    PlanAttemptSpec(
                    index=next_index,
                    mode="fast",
                    reasoner=args.plan_fallback_reasoner.strip(),
                    timeout=effective_plan_fallback_timeout(args),
                    label="fallback-fast",
                )
                )
    return attempts


def merge_config_into_args(args: argparse.Namespace, cfg: dict) -> argparse.Namespace:
    """
    CLI overrides config. Config only fills unset/default-ish values.
    """
    def has(field: str) -> bool:
        return hasattr(args, field)

    def get(field: str, default=None):
        return getattr(args, field, default)

    def set_(field: str, value):
        setattr(args, field, value)

    mappings = {
        "repo": "repo",
        "goals": "goals",
        "ai_dir": "ai_dir",
        "extra_context": "extra_context",
        "max_tree_lines": "max_tree_lines",
        "reasoner": "reasoner",
        "aider_model": "aider_model",
        "test_cmd": "test_cmd",
        "lint_cmd": "lint_cmd",
        "max_iters": "max_iters",
        "zip": "zip",
        "branch": "branch",
        "base": "base",
        "base_branch": "base",
        "allowed_file": "allowed_file",
        "allowed_files": "allowed_file",
        "aider_extra_arg": "aider_extra_arg",
        "aider_extra_args": "aider_extra_arg",
        "lint_required": "lint_required",
        "allow_dirty_repo": "allow_dirty_repo",
        "reasoner_timeout": "reasoner_timeout",
        "report_timeout": "report_timeout",
        "aider_timeout": "aider_timeout",
        "test_timeout": "test_timeout",
        "plan_mode": "plan_mode",
        "plan_max_tree_lines": "plan_max_tree_lines",
        "plan_max_files": "plan_max_files",
        "plan_max_file_chars": "plan_max_file_chars",
        "plan_max_changes": "plan_max_changes",
        "plan_fallback_reasoner": "plan_fallback_reasoner",
        "plan_fallback_timeout": "plan_fallback_timeout",
        "plan_retry_on_timeout": "plan_retry_on_timeout",
        "apply_max_plan_changes": "apply_max_plan_changes",
        "apply_limit_to_plan_files": "apply_limit_to_plan_files",
        "apply_enforce_plan_scope": "apply_enforce_plan_scope",
        "apply_skip_unsupported_plan_changes": "apply_skip_unsupported_plan_changes",
        "semantic_validation": "semantic_validation",
        "validation_profile": "validation_profile",
        "semantic_validation_mode": "semantic_validation_mode",
        "semantic_validation_strict": "semantic_validation_strict",
        "semantic_validation_timeout": "semantic_validation_timeout",
    }

    for ck, af in mappings.items():
        if ck not in cfg or not has(af):
            continue

        val = cfg[ck]
        cur = get(af)

        if isinstance(cur, str):
            default_strings = {
                "plan_mode": "balanced",
                "plan_fallback_reasoner": "",
                "validation_profile": "auto",
                "semantic_validation_mode": "auto",
            }
            if not str(cur).strip() or (af in default_strings and cur == default_strings[af]):
                set_(af, str(val))
        elif isinstance(cur, list):
            if not cur and isinstance(val, list):
                set_(af, val)
        elif isinstance(cur, bool):
            if cur is False and bool(val) is True:
                set_(af, True)
        elif isinstance(cur, int):
            defaults = {
                "max_iters": 2,
                "max_tree_lines": 600,
                "reasoner_timeout": 1800,
                "report_timeout": 0,
                "aider_timeout": 7200,
                "test_timeout": 1800,
                "plan_max_tree_lines": 0,
                "plan_max_files": 0,
                "plan_max_file_chars": 0,
                "plan_max_changes": 0,
                "plan_fallback_timeout": 0,
                "apply_max_plan_changes": 0,
                "semantic_validation_timeout": 60,
            }
            if af in defaults and cur == defaults[af]:
                set_(af, int(val))
            elif cur in (0, None):
                set_(af, int(val))
        else:
            if cur is None:
                set_(af, val)

    return args


# -----------------------------
# Model interaction
# -----------------------------

@dataclasses.dataclass
class ModelSpec:
    kind: str  # "ollama" or "cmd"
    name: str  # model name or command template


@dataclasses.dataclass
class PlanSettings:
    mode: str
    max_tree_lines: int
    max_files: int
    max_file_chars: int
    max_changes: int


@dataclasses.dataclass
class PlanAttemptSpec:
    index: int
    mode: str
    reasoner: str
    timeout: int
    label: str


@dataclasses.dataclass
class PlanChange:
    index: int
    title: str
    files: List[str]
    body: str


@dataclasses.dataclass
class ApplyScope:
    selected_changes: List[PlanChange]
    selected_files: List[str]
    selected_plan_md: str
    max_changes: int
    source: str
    skipped_change_titles: List[str]


@dataclasses.dataclass
class FunctionSignature:
    name: str
    required_positional: int
    max_positional: Optional[int]
    keyword_only_required: List[str]


@dataclasses.dataclass
class ValidationContext:
    is_python_repo: bool
    has_tests: bool
    has_pyproject: bool
    has_requirements: bool
    has_src_dir: bool
    has_main_py: bool
    has_dot_venv: bool
    has_uv_lock: bool
    has_setup_cfg: bool
    python_runner: Optional[str]
    python_runner_source: str
    runner_reproducible: bool
    runner_usable: bool
    pytest_available: bool
    pytest_command: str
    compile_command: str
    repo_notes: List[str]
    suggested_profile: str
    suggestion_reason: str
    promotion_reasons: List[str]
    promotion_blockers: List[str]


@dataclasses.dataclass
class ValidationPlan:
    profile: str
    source: str
    suggested_profile: str
    suggestion_reason: str
    promotion_decision: str
    promotion_reasons: List[str]
    promotion_blockers: List[str]
    test_command: str
    lint_command: Optional[str]
    semantic_validation: bool
    semantic_validation_mode: str
    semantic_validation_strict: bool
    semantic_validation_timeout: int
    smoke_imports: bool
    smoke_imports_strict: bool
    smoke_import_runner: Optional[str]
    checks: List[Dict[str, Any]]
    context: ValidationContext


def parse_model_spec(s: str) -> ModelSpec:
    if s.startswith("ollama:"):
        return ModelSpec(kind="ollama", name=s.split(":", 1)[1].strip())
    if s.startswith("cmd:"):
        return ModelSpec(kind="cmd", name=s.split(":", 1)[1].strip())
    raise ValueError("Model must be specified as ollama:<model> or cmd:<...>")


def normalize_aider_model_name(model: str) -> str:
    model = model.strip()
    if model.startswith("ollama:"):
        return "ollama/" + model.split(":", 1)[1]
    return model


def parse_plan_changes(plan_md: str) -> List[PlanChange]:
    changes: List[PlanChange] = []
    pattern = re.compile(r"(?ms)^(\d+)\)\s+Change:\s*(.+?)\n(.*?)(?=^\d+\)\s+Change:|\Z)")
    for match in pattern.finditer(plan_md):
        index = int(match.group(1))
        title = match.group(2).strip()
        body = match.group(0).strip()
        details = match.group(3)
        files_line_match = re.search(r"^\s*-\s*Files:\s*(.+)$", details, re.MULTILINE)
        files: List[str] = []
        if files_line_match:
            files_field = files_line_match.group(1).strip()
            backtick_files = re.findall(r"`([^`]+)`", files_field)
            candidates = backtick_files or re.split(r",|\band\b", files_field)
            for candidate in candidates:
                cleaned = re.sub(r"\(.*?\)", "", candidate).strip(" `*.-")
                if not cleaned or cleaned.lower() in {"etc", "etc."}:
                    continue
                if "/" in cleaned or "." in cleaned:
                    files.append(cleaned)
        changes.append(PlanChange(index=index, title=title, files=list(dict.fromkeys(files)), body=body))
    return changes


def effective_apply_max_plan_changes(args) -> int:
    if getattr(args, "apply_max_plan_changes", 0):
        return int(args.apply_max_plan_changes)
    return 1 if getattr(args, "plan_mode", "balanced") == "fast" else 0


def load_repo_dependency_names(repo: Path) -> set[str]:
    deps: set[str] = set()
    requirements = repo / "requirements.txt"
    if requirements.exists():
        for raw_line in requirements.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[<>=!~\[]", line, maxsplit=1)[0].strip().lower()
            if pkg:
                deps.add(pkg)
    return deps


DECLARED_DEP_ALIASES = {
    "pillow": {"pil"},
}


def declared_import_names(repo: Path) -> set[str]:
    names: set[str] = set()
    for dep in load_repo_dependency_names(repo):
        names.add(dep)
        names.add(dep.replace("-", "_"))
        names.update(DECLARED_DEP_ALIASES.get(dep, set()))
    return names


def extract_import_roots(source: str) -> set[str]:
    roots: set[str] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return roots
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or not node.module:
                continue
            roots.add(node.module.split(".", 1)[0])
    return roots


def audit_python_dependency_declarations(repo: Path, changed_files: List[str]) -> Dict[str, Any]:
    declared = declared_import_names(repo)
    stdlib = {name.lower() for name in getattr(sys, "stdlib_module_names", set())}
    local_roots = {path.parts[1] for path in (repo / "src").glob("*") if path.is_dir()} if (repo / "src").exists() else set()
    issues: List[Dict[str, Any]] = []
    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue
        path = repo / rel_path
        if not path.exists():
            continue
        imports = extract_import_roots(read_text(path))
        missing = []
        for root in sorted(imports):
            lowered = root.lower()
            if lowered in stdlib or lowered in declared or root in local_roots:
                continue
            missing.append(root)
        if missing:
            issues.append({"file": rel_path, "missing_dependencies": missing})
    return {"ok": not issues, "issues": issues}


def build_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> FunctionSignature:
    positional = len(node.args.posonlyargs) + len(node.args.args)
    defaults = len(node.args.defaults)
    required_positional = max(0, positional - defaults)
    max_positional = None if node.args.vararg else positional
    keyword_only_required = [
        arg.arg for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is None
    ]
    return FunctionSignature(
        name=node.name,
        required_positional=required_positional,
        max_positional=max_positional,
        keyword_only_required=keyword_only_required,
    )


def collect_local_function_signatures(tree: ast.AST) -> Dict[str, FunctionSignature]:
    signatures: Dict[str, FunctionSignature] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures[node.name] = build_function_signature(node)
    return signatures


def module_path_from_import(repo: Path, current_file: Path, module: Optional[str], level: int) -> Optional[Path]:
    try:
        rel_parts = current_file.relative_to(repo).parts
    except ValueError:
        return None
    if level > 0:
        package_parts = list(rel_parts[:-1])
        if level > len(package_parts):
            return None
        package_parts = package_parts[: len(package_parts) - level + 1]
        if module:
            package_parts.extend(module.split("."))
        candidate = repo.joinpath(*package_parts)
    else:
        if not module:
            return None
        candidate = repo.joinpath(*module.split("."))
    py_candidate = candidate.with_suffix(".py")
    init_candidate = candidate / "__init__.py"
    if py_candidate.exists():
        return py_candidate
    if init_candidate.exists():
        return init_candidate
    return None


def resolve_imported_local_signatures(repo: Path, rel_path: str, tree: ast.AST) -> Tuple[Dict[str, FunctionSignature], List[Dict[str, Any]], set[str]]:
    signatures: Dict[str, FunctionSignature] = {}
    import_issues: List[Dict[str, Any]] = []
    imported_names: set[str] = set()
    file_path = repo / rel_path
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
            module_path = module_path_from_import(repo, file_path, node.module, node.level)
            if not module_path:
                if node.level > 0:
                    import_issues.append(
                        {
                            "kind": "local_import_unresolved",
                            "severity": "failed",
                            "file": rel_path,
                            "message": f"No se pudo resolver el import local: from {'.' * node.level}{node.module or ''} import ...",
                        }
                    )
                continue
            source = read_text(module_path)
            try:
                imported_tree = ast.parse(source)
            except SyntaxError as exc:
                import_issues.append(
                    {
                        "kind": "local_import_syntax_error",
                        "severity": "failed",
                        "file": rel_path,
                        "message": f"El módulo importado {module_path.relative_to(repo)} no parsea: {exc}",
                    }
                )
                continue
            imported_signatures = collect_local_function_signatures(imported_tree)
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name in imported_signatures:
                    signatures[alias.asname or alias.name] = imported_signatures[alias.name]
    return signatures, import_issues, imported_names


def run_optional_semantic_tool(
    repo: Path,
    files: List[str],
    mode: str,
    timeout: int,
) -> Dict[str, Any]:
    tool_cmd: Optional[List[str]] = None
    tool_name = ""
    if mode in {"auto", "ruff"} and command_exists("ruff"):
        tool_name = "ruff"
        tool_cmd = ["ruff", "check", "--select", "F821,F822,F823", *files]
    elif mode in {"auto", "pyflakes"} and command_exists("pyflakes"):
        tool_name = "pyflakes"
        tool_cmd = ["pyflakes", *files]
    else:
        return {"tool": None, "status": "skipped", "issues": [], "message": "No optional semantic tool available."}

    try:
        rc, out, err = run_cmd(tool_cmd, cwd=repo, capture=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {
            "tool": tool_name,
            "status": "warning",
            "issues": [],
            "message": f"{tool_name} timed out after {timeout}s.",
        }
    output = "\n".join(part for part in [out.strip(), err.strip()] if part).strip()
    if rc == 0:
        return {"tool": tool_name, "status": "ok", "issues": [], "message": output}
    return {
        "tool": tool_name,
        "status": "failed",
        "issues": [{"kind": "optional_tool", "severity": "failed", "message": output or f"{tool_name} reported issues."}],
        "message": output,
    }


def python_module_name_for_path(rel_path: str) -> Optional[str]:
    path = Path(rel_path)
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def run_python_smoke_imports(
    repo: Path,
    changed_files: List[str],
    runner: Optional[str],
    timeout: int,
) -> Dict[str, Any]:
    if not runner:
        return {"status": "skipped", "issues": [], "warnings": ["No Python runner available for smoke imports."], "modules": []}
    modules = [python_module_name_for_path(path) for path in changed_files if path.endswith(".py")]
    modules = [module for module in modules if module]
    if not modules:
        return {"status": "skipped", "issues": [], "warnings": ["No changed Python modules to import."], "modules": []}
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(repo), str(repo / "src"), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    issues: List[Dict[str, Any]] = []
    for module in modules:
        try:
            rc, out, err = run_cmd([runner, "-c", f"import importlib; importlib.import_module('{module}')"], cwd=repo, capture=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"status": "warning", "issues": [], "warnings": [f"Smoke import timed out after {timeout}s."], "modules": modules}
        if rc != 0:
            issues.append(
                {
                    "kind": "smoke_import_failed",
                    "severity": "failed",
                    "module": module,
                    "message": truncate((out + "\n" + err).strip(), 2000),
                }
            )
    return {
        "status": "failed" if issues else "ok",
        "issues": issues,
        "warnings": [],
        "modules": modules,
    }


def run_semantic_validation(
    repo: Path,
    changed_files: List[str],
    *,
    enabled: bool,
    mode: str,
    strict: bool,
    timeout: int,
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "enabled": enabled,
        "mode": mode,
        "strict": strict,
        "timeout_seconds": timeout,
        "status": "skipped",
        "files_checked": [],
        "issues": [],
        "warnings": [],
        "tools": [],
    }
    if not enabled:
        summary["warnings"].append("Semantic validation disabled.")
        return summary

    python_files = [path for path in changed_files if path.endswith(".py")]
    summary["files_checked"] = python_files
    if not python_files:
        summary["warnings"].append("No changed Python files to validate.")
        return summary

    for rel_path in python_files:
        source = read_text(repo / rel_path)
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            summary["issues"].append(
                {
                    "kind": "syntax_error",
                    "severity": "failed",
                    "file": rel_path,
                    "message": str(exc),
                }
            )
            continue

        local_sigs = collect_local_function_signatures(tree)
        imported_sigs, import_issues, imported_names = resolve_imported_local_signatures(repo, rel_path, tree)
        summary["issues"].extend(import_issues)
        known_callables = {**local_sigs, **imported_sigs}
        module_names = set(local_sigs) | imported_names | set(dir(builtins))

        class SemanticVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scopes: List[set[str]] = [set(module_names)]

            def _all_names(self) -> set[str]:
                combined: set[str] = set()
                for scope in self.scopes:
                    combined.update(scope)
                return combined

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                params = {arg.arg for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}
                if node.args.vararg:
                    params.add(node.args.vararg.arg)
                if node.args.kwarg:
                    params.add(node.args.kwarg.arg)
                self.scopes.append(params)
                self.generic_visit(node)
                self.scopes.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Assign(self, node: ast.Assign) -> None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.scopes[-1].add(target.id)
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if isinstance(node.target, ast.Name):
                    self.scopes[-1].add(node.target.id)
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                if isinstance(node.func, ast.Name):
                    callee = node.func.id
                    if any(keyword.arg is None for keyword in node.keywords):
                        return
                    if callee in known_callables:
                        sig = known_callables[callee]
                        provided_positional = len(node.args)
                        provided_keyword_names = {kw.arg for kw in node.keywords if kw.arg}
                        missing_required = max(
                            0,
                            sig.required_positional - provided_positional - len([name for name in provided_keyword_names if name])
                        )
                        if sig.max_positional is not None and provided_positional > sig.max_positional:
                            summary["issues"].append(
                                {
                                    "kind": "call_arity_mismatch",
                                    "severity": "failed",
                                    "file": rel_path,
                                    "message": f"Llamada a {callee} con demasiados argumentos posicionales ({provided_positional}); máximo {sig.max_positional}.",
                                    "line": node.lineno,
                                }
                            )
                        elif missing_required > 0:
                            summary["issues"].append(
                                {
                                    "kind": "call_arity_mismatch",
                                    "severity": "failed",
                                    "file": rel_path,
                                    "message": f"Llamada a {callee} con argumentos faltantes; requiere {sig.required_positional} posicionales.",
                                    "line": node.lineno,
                                }
                            )
                    elif callee not in self._all_names():
                        summary["issues"].append(
                            {
                                "kind": "undefined_call_target",
                                "severity": "failed",
                                "file": rel_path,
                                "message": f"Llamada a nombre no definido: {callee}",
                                "line": node.lineno,
                            }
                        )
                self.generic_visit(node)

        SemanticVisitor().visit(tree)

    dependency_audit = audit_python_dependency_declarations(repo, python_files)
    if not dependency_audit["ok"]:
        for issue in dependency_audit["issues"]:
            summary["issues"].append(
                {
                    "kind": "undeclared_dependency",
                    "severity": "failed",
                    "file": issue["file"],
                    "message": f"Imports sin dependencia declarada: {', '.join(issue['missing_dependencies'])}",
                }
            )

    optional_tool = run_optional_semantic_tool(repo, python_files, mode, timeout)
    summary["tools"].append(optional_tool)
    if optional_tool["status"] == "warning" and optional_tool.get("message"):
        summary["warnings"].append(optional_tool["message"])
    summary["issues"].extend(optional_tool.get("issues", []))

    failed_issues = [issue for issue in summary["issues"] if issue.get("severity") == "failed"]
    if failed_issues:
        summary["status"] = "failed" if strict else "warning"
    elif summary["warnings"]:
        summary["status"] = "warning"
    else:
        summary["status"] = "ok"
    return summary


def change_dependency_refs(change: PlanChange) -> List[str]:
    refs: List[str] = []
    for token in re.findall(r"`([^`]+)`", change.body):
        cleaned = token.strip().rstrip("()").lower()
        if not cleaned or "/" in cleaned or "." in cleaned:
            continue
        refs.append(cleaned)
    return list(dict.fromkeys(refs))


def change_is_repo_supported(repo: Path, change: PlanChange) -> bool:
    declared_deps = load_repo_dependency_names(repo)
    for dep in change_dependency_refs(change):
        if dep and dep not in declared_deps:
            return False
    listed_files = [path for path in change.files if path]
    if listed_files and not any((repo / rel_path).exists() for rel_path in listed_files):
        return False
    return True


def resolve_apply_scope(repo: Path, plan_md: str, args) -> ApplyScope:
    changes = parse_plan_changes(plan_md)
    max_changes = effective_apply_max_plan_changes(args)
    skipped_change_titles: List[str] = []
    candidate_changes = changes
    if getattr(args, "apply_skip_unsupported_plan_changes", False):
        supported_changes: List[PlanChange] = []
        for change in changes:
            if change_is_repo_supported(repo, change):
                supported_changes.append(change)
            else:
                skipped_change_titles.append(change.title)
        if supported_changes:
            candidate_changes = supported_changes
    selected_changes = candidate_changes[:max_changes] if max_changes > 0 else candidate_changes
    selected_plan_md = "\n\n".join(change.body for change in selected_changes).strip()
    selected_files: List[str] = []
    if getattr(args, "allowed_file", None):
        selected_files = list(dict.fromkeys(args.allowed_file))
        source = "cli"
    elif getattr(args, "apply_limit_to_plan_files", True):
        for change in selected_changes:
            for rel_path in change.files:
                if (repo / rel_path).exists():
                    selected_files.append(rel_path)
        selected_files = list(dict.fromkeys(selected_files))
        source = "plan"
    else:
        source = "unrestricted"
    return ApplyScope(
        selected_changes=selected_changes,
        selected_files=selected_files,
        selected_plan_md=selected_plan_md,
        max_changes=max_changes,
        source=source,
        skipped_change_titles=skipped_change_titles,
    )


def stringify_command(args: List[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def call_model(model: ModelSpec, prompt: str, cwd: Optional[Path] = None, timeout: int = 1800) -> str:
    if model.kind == "ollama":
        args = ["ollama", "run", model.name]
    else:
        args = shlex.split(model.name)

    try:
        proc = subprocess.run(
            args,
            input=prompt,
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutionFailure(
            f"Model call timed out after {timeout}s.",
            command=stringify_command(args),
            stdout=to_text(exc.stdout),
            stderr=to_text(exc.stderr),
            timeout_seconds=timeout,
            status="timeout",
        ) from exc
    if proc.returncode != 0:
        raise ExecutionFailure(
            f"Model call failed ({proc.returncode}).",
            command=stringify_command(args),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            status="failed",
        )
    return proc.stdout


def validate_model_available(model: ModelSpec) -> None:
    if model.kind == "ollama":
        if not command_exists("ollama"):
            raise SystemExit("No se encontró 'ollama' en PATH.")
        rc, out, err = run_cmd(["ollama", "list"], capture=True, timeout=60)
        if rc != 0:
            raise SystemExit(f"No pude consultar 'ollama list'.\n{err}")
        available = {line.split()[0] for line in out.splitlines()[1:] if line.strip()}
        if model.name not in available and f"{model.name}:latest" not in available:
            raise SystemExit(f"El modelo Ollama no parece estar disponible: {model.name}")
    elif model.kind == "cmd":
        first = shlex.split(model.name)[0]
        if not command_exists(first):
            raise SystemExit(f"No se encontró el ejecutable del modelo cmd: {first}")


def effective_report_timeout(args) -> int:
    return args.report_timeout or args.reasoner_timeout


def save_model_artifacts(ai_dir: Path, prefix: str, prompt: str, raw: Optional[str] = None) -> None:
    safe_write(ai_dir / f"{prefix}-prompt.txt", prompt)
    if raw is not None:
        safe_write(ai_dir / f"{prefix}-raw.txt", raw)


def write_stage_error(ai_dir: Path, stage: str, exc: BaseException, extra: Optional[Dict[str, Any]] = None) -> Path:
    error_path = ai_dir / f"{stage}-error.md"
    details: Dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, ExecutionFailure):
        details.update(exc.to_dict())
    elif isinstance(exc, subprocess.TimeoutExpired):
        details.update(
            {
                "command": to_text(exc.cmd),
                "timeout_seconds": exc.timeout,
                "stdout": truncate(to_text(exc.stdout), 12000),
                "stderr": truncate(to_text(exc.stderr), 12000),
                "status": "timeout",
            }
        )
    if extra:
        details.update(extra)

    parts = [
        f"# {stage.title()} Error",
        "",
        f"- error_type: `{details.get('error_type', type(exc).__name__)}`",
        f"- message: {details.get('message', str(exc))}",
    ]
    for key in ("status", "command", "returncode", "timeout_seconds"):
        if details.get(key) not in (None, ""):
            parts.append(f"- {key}: `{details[key]}`")
    if details.get("stdout"):
        parts.append("\n## STDOUT\n```text\n" + details["stdout"] + "\n```")
    if details.get("stderr"):
        parts.append("\n## STDERR\n```text\n" + details["stderr"] + "\n```")
    context_keys = [k for k in details if k not in {"error_type", "message", "status", "command", "returncode", "timeout_seconds", "stdout", "stderr"}]
    if context_keys:
        context = {k: details[k] for k in context_keys}
        parts.append("\n## Context\n```json\n" + json.dumps(context, indent=2, ensure_ascii=False) + "\n```")
    safe_write(error_path, "\n".join(parts) + "\n")
    return error_path


@contextlib.contextmanager
def record_stage(recorder: RunRecorder, stage_name: str):
    recorder.start_stage(stage_name)
    stage_details: Dict[str, Any] = {}
    try:
        yield stage_details
    except BaseException as exc:
        status = "timeout" if isinstance(exc, (subprocess.TimeoutExpired,)) else "failed"
        if isinstance(exc, ExecutionFailure):
            status = exc.status
        recorder.finish_stage(stage_name, status, stage_details)
        raise
    else:
        status = stage_details.pop("__status", "ok")
        recorder.finish_stage(stage_name, status, stage_details)


# -----------------------------
# Git helpers
# -----------------------------

def git_current_branch(repo: Path) -> str:
    _, out, _ = run_cmd("git rev-parse --abbrev-ref HEAD", cwd=repo, check=True)
    return out.strip()


def git_head_commit(repo: Path) -> str:
    _, out, _ = run_cmd("git rev-parse HEAD", cwd=repo, check=True)
    return out.strip()


def git_changed_files_between(repo: Path, base_ref: str, head_ref: str = "HEAD") -> List[str]:
    code, out, _ = run_cmd(["git", "diff", "--name-only", f"{base_ref}..{head_ref}"], cwd=repo)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def git_checkout(repo: Path, branch: str) -> None:
    run_cmd(f"git checkout {shlex.quote(branch)}", cwd=repo, check=True)


def git_create_branch(repo: Path, branch_name: str) -> None:
    run_cmd(f"git checkout -b {shlex.quote(branch_name)}", cwd=repo, check=True)


def git_status_porcelain(repo: Path) -> str:
    _, out, _ = run_cmd("git status --porcelain", cwd=repo)
    return out.strip()


def git_ref_exists(repo: Path, ref: str) -> bool:
    code, _, _ = run_cmd(["git", "rev-parse", "--verify", ref], cwd=repo)
    return code == 0


def git_archive_zip(repo: Path, out_zip: Path, ref: str = "HEAD") -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "archive", "--format=zip", "-o", str(out_zip), ref], cwd=repo, check=True)


def resolve_base_branch(repo: Path, requested_base: str = "") -> str:
    requested_base = (requested_base or "").strip()
    if requested_base:
        if not git_ref_exists(repo, requested_base):
            raise SystemExit(f"La rama/ref base no existe: {requested_base}")
        return requested_base

    current = git_current_branch(repo)
    if current.startswith("ai/"):
        for candidate in ("main", "master"):
            if git_ref_exists(repo, candidate):
                return candidate
    return current


def infer_base_ref(repo: Path) -> str:
    code, out, _ = run_cmd("git rev-parse --abbrev-ref --symbolic-full-name @{u}", cwd=repo)
    if code == 0:
        upstream = out.strip()
        code2, out2, _ = run_cmd(f"git merge-base HEAD {shlex.quote(upstream)}", cwd=repo)
        if code2 == 0 and out2.strip():
            return out2.strip()
    return "HEAD~1"


def collect_git_diff(repo: Path, base_ref: str) -> str:
    code, out, err = run_cmd(f"git diff {shlex.quote(base_ref)}...HEAD", cwd=repo, capture=True)
    if code != 0:
        return f"[git diff failed]\n{err}"
    return truncate(out, 20000)


def collect_git_changed_files(repo: Path, base_ref: str) -> List[str]:
    code, out, _ = run_cmd(["git", "diff", "--name-only", f"{base_ref}...HEAD"], cwd=repo, capture=True)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def infer_validation_changed_files(repo: Path, args) -> Tuple[List[str], str]:
    base_ref = (getattr(args, "base_ref", "") or "").strip()
    if not base_ref:
        try:
            base_ref = infer_base_ref(repo)
        except Exception:
            base_ref = "HEAD~1"
    changed = collect_git_changed_files(repo, base_ref)
    if changed:
        return changed, base_ref
    code, out, _ = run_cmd(["git", "diff", "--name-only", "HEAD"], cwd=repo, capture=True)
    if code == 0:
        fallback = [line.strip() for line in out.splitlines() if line.strip()]
        if fallback:
            return fallback, "HEAD"
    return [], base_ref


def validation_plan_to_dict(plan: ValidationPlan) -> Dict[str, Any]:
    return {
        "profile": plan.profile,
        "source": plan.source,
        "suggested_profile": plan.suggested_profile,
        "suggestion_reason": plan.suggestion_reason,
        "promotion_decision": plan.promotion_decision,
        "promotion_reasons": plan.promotion_reasons,
        "promotion_blockers": plan.promotion_blockers,
        "runner": {
            "python": plan.context.python_runner,
            "source": plan.context.python_runner_source,
            "reproducible": plan.context.runner_reproducible,
            "usable": plan.context.runner_usable,
        },
        "test_command": plan.test_command,
        "lint_command": plan.lint_command,
        "semantic_validation": {
            "enabled": plan.semantic_validation,
            "mode": plan.semantic_validation_mode,
            "strict": plan.semantic_validation_strict,
            "timeout_seconds": plan.semantic_validation_timeout,
        },
        "smoke_imports": {
            "enabled": plan.smoke_imports,
            "strict": plan.smoke_imports_strict,
            "runner": plan.smoke_import_runner,
        },
        "checks": plan.checks,
        "context": dataclasses.asdict(plan.context),
    }


# -----------------------------
# Aider preparation / interaction
# -----------------------------

DEFAULT_AIDER_GITIGNORE_PATTERNS = [
    ".aider*",
    ".env",
    ".venv/",
    "venv/",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
]


AIDER_FATAL_MARKERS = [
    "litellm.BadRequestError",
    "LLM Provider NOT provided",
    "Traceback (most recent call last):",
]


def ensure_repo_ready_for_aider(repo: Path) -> None:
    """
    Make the target repo less likely to trigger interactive Aider prompts,
    especially around .gitignore.
    """
    gitignore = repo / ".gitignore"
    existing = read_text(gitignore, default="")
    missing = [p for p in DEFAULT_AIDER_GITIGNORE_PATTERNS if p not in existing]

    if missing:
        parts = [existing.rstrip()] if existing.strip() else []
        parts.append("# Aider / local artifacts")
        parts.extend(missing)
        new_content = "\n".join(parts).rstrip() + "\n"
        safe_write(gitignore, new_content)


def run_aider(
    repo: Path,
    aider_model: str,
    message: str,
    files: Optional[List[str]] = None,
    log_path: Optional[Path] = None,
    extra_args: Optional[List[str]] = None,
    timeout: int = 7200,
) -> Tuple[int, str, str]:
    """
    Run Aider in as non-interactive a way as possible.
    """
    base_cmd = [
        "aider",
        "--model", normalize_aider_model_name(aider_model),
        "--auto-commits",
        "--yes-always",
        "--no-show-model-warnings",
        "--no-check-model-accepts-settings",
    ]

    if files:
        for f in files:
            base_cmd += ["--file", f]

    base_cmd += ["--message", message]

    if extra_args:
        base_cmd += extra_args

    try:
        proc = subprocess.run(
            base_cmd,
            cwd=str(repo),
            text=True,
            capture_output=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = to_text(exc.stdout)
        stderr = to_text(exc.stderr)
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            existing = read_text(log_path, default="")
            block = (
                f"{existing}\n\n" if existing.strip() else ""
            ) + (
                f"# Aider run ({now_stamp()})\n\n"
                f"## CMD\n```bash\n{stringify_command(base_cmd)}\n```\n\n"
                f"## RESULT\nTimed out after {timeout}s\n\n"
                f"## STDOUT\n```text\n{stdout}\n```\n\n"
                f"## STDERR\n```text\n{stderr}\n```\n"
            )
            safe_write(log_path, block)
        raise ExecutionFailure(
            f"Aider timed out after {timeout}s.",
            command=stringify_command(base_cmd),
            stdout=stdout,
            stderr=stderr,
            timeout_seconds=timeout,
            status="timeout",
        ) from exc

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_text(log_path, default="")
        block = (
            f"{existing}\n\n" if existing.strip() else ""
        ) + (
            f"# Aider run ({now_stamp()})\n\n"
            f"## CMD\n```bash\n{stringify_command(base_cmd)}\n```\n\n"
            f"## RETURN CODE\n{proc.returncode}\n\n"
            f"## STDOUT\n```text\n{proc.stdout}\n```\n\n"
            f"## STDERR\n```text\n{proc.stderr}\n```\n"
        )
        safe_write(log_path, block)

    return proc.returncode, proc.stdout, proc.stderr


def aider_output_has_fatal_error(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}"
    return any(marker in combined for marker in AIDER_FATAL_MARKERS)


# -----------------------------
# Reasoner / report prompts
# -----------------------------

PLAN_TEMPLATE = """\
# Repo Review Plan

## Summary
- (fill)

## Assumptions
- (fill)

## Changes (ordered)
1) Change: ...
   - Files: ...
   - Rationale: ...
   - Acceptance criteria:
     - ...
   - Tests:
     - ...

## Non-goals
- (fill)

## Commands to validate
- (fill exact commands)
"""


FAST_PLAN_STRUCTURE = """\
# Repo Review Plan
## Summary
- ...
## Assumptions
- ...
## Changes (ordered)
1) Change: ...
   - Files: ...
   - Rationale: ...
   - Acceptance criteria:
     - ...
   - Tests:
     - ...
## Non-goals
- ...
## Commands to validate
- ...
"""


def select_plan_context_files(repo: Path, extra_context_files: List[str], max_files: int) -> List[str]:
    key_candidates = [
        "README.md",
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "Makefile",
        "justfile",
        "setup.cfg",
        "Cargo.toml",
        "go.mod",
        "main.py",
        ".github/workflows/ci.yml",
        ".github/workflows/test.yml",
    ]
    selected: List[str] = []
    for candidate in key_candidates + extra_context_files:
        if candidate and candidate not in selected and (repo / candidate).exists():
            selected.append(candidate)
        if len(selected) >= max_files:
            break
    return selected


def detect_validation_commands(repo: Path, args) -> List[str]:
    commands: List[str] = []
    for candidate in [getattr(args, "lint_cmd", ""), getattr(args, "test_cmd", "")]:
        if str(candidate or "").strip():
            commands.append(str(candidate).strip())
    if commands:
        return commands

    try:
        validation_plan = resolve_validation_plan(repo, args)
        if validation_plan.lint_command:
            commands.append(validation_plan.lint_command)
        if validation_plan.test_command:
            commands.append(validation_plan.test_command)
        if commands:
            return commands[:4]
    except Exception:
        pass

    if (repo / "tests").exists() or (repo / "pytest.ini").exists():
        commands.append("pytest -q")
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        compile_parts = []
        if (repo / "src").exists():
            compile_parts.append("src")
        if (repo / "main.py").exists():
            compile_parts.append("main.py")
        if compile_parts:
            commands.append(f"python3 -m compileall {' '.join(compile_parts)}")
    if (repo / "package.json").exists():
        commands.append("npm test")
    if (repo / "Cargo.toml").exists():
        commands.append("cargo test")
    if (repo / "go.mod").exists():
        commands.append("go test ./...")
    return commands[:4]


def repo_python_runner(repo: Path) -> Tuple[Optional[str], str]:
    candidates = [
        (repo / ".venv" / "bin" / "python", ".venv"),
        (repo / ".venv" / "Scripts" / "python.exe", ".venv"),
    ]
    for path, source in candidates:
        if path.exists():
            return str(path), source
    if command_exists("python3"):
        return "python3", "system"
    if command_exists("python"):
        return "python", "system"
    return None, "missing"


def python_module_available(runner: Optional[str], module: str, repo: Path, timeout: int = 10) -> bool:
    if not runner:
        return False
    try:
        rc, _, _ = run_cmd([runner, "-c", f"import {module}"], cwd=repo, capture=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return rc == 0


def runner_executes_python(runner: Optional[str], repo: Path, timeout: int = 10) -> bool:
    if not runner:
        return False
    try:
        rc, _, _ = run_cmd([runner, "-c", "print('ok')"], cwd=repo, capture=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return rc == 0


def detect_validation_context(repo: Path) -> ValidationContext:
    has_pyproject = (repo / "pyproject.toml").exists()
    has_requirements = (repo / "requirements.txt").exists()
    has_setup_cfg = (repo / "setup.cfg").exists()
    has_src_dir = (repo / "src").exists()
    has_main_py = (repo / "main.py").exists()
    has_tests = (repo / "tests").exists() or (repo / "pytest.ini").exists()
    has_dot_venv = (repo / ".venv").exists()
    has_uv_lock = (repo / "uv.lock").exists()
    is_python_repo = any([has_pyproject, has_requirements, has_setup_cfg, has_src_dir, has_main_py])
    runner, runner_source = repo_python_runner(repo)
    runner_reproducible = runner_source == ".venv"
    runner_usable = runner_executes_python(runner, repo) if runner else False
    pytest_available = python_module_available(runner, "pytest", repo) if runner else False
    repo_notes: List[str] = []
    if has_dot_venv:
        repo_notes.append("Detected .venv in target repo.")
    if has_uv_lock:
        repo_notes.append("Detected uv.lock in target repo.")
        repo_notes.append("uv.lock is only supporting evidence; it is not used as an execution runner by itself.")
    if not has_dot_venv:
        repo_notes.append("No repo-local virtual environment detected.")
    if runner and not runner_usable:
        repo_notes.append("Detected Python runner did not execute successfully.")
    if has_tests and not pytest_available:
        repo_notes.append("Tests exist but pytest is not available in the detected Python runner.")
    compile_target = []
    if has_src_dir:
        compile_target.append("src")
    if has_main_py:
        compile_target.append("main.py")
    compile_command = f"{runner} -m compileall {' '.join(compile_target)}".strip() if runner and compile_target else ""
    pytest_command = ""
    if has_tests and pytest_available and runner:
        pytest_command = f"{runner} -m pytest -q"
    promotion_reasons: List[str] = []
    promotion_blockers: List[str] = []
    if is_python_repo:
        if runner_reproducible:
            promotion_reasons.append("Repo-local Python runner detected in .venv.")
        else:
            promotion_blockers.append("No reproducible repo-local Python runner was found.")
        if runner_usable:
            promotion_reasons.append("The selected Python runner executes successfully.")
        else:
            promotion_blockers.append("The selected Python runner is not executable.")
        if compile_command:
            promotion_reasons.append("The runner can execute a repo-scoped compile command.")
        else:
            promotion_blockers.append("No repo-scoped Python validation command could be derived.")
        if has_tests:
            if pytest_command:
                promotion_reasons.append("pytest is available inside the selected runner.")
            else:
                repo_notes.append("pytest-based repo validation is not available; promotion may still use smoke imports only.")
        else:
            repo_notes.append("Repo has no test suite; python_repo, if promoted, will rely on smoke imports.")
    if is_python_repo and runner_reproducible and runner_usable and compile_command:
        suggested_profile = "python_repo"
        suggestion_reason = "Python repo with a reproducible local runner and repo-scoped validation commands."
    elif is_python_repo:
        suggested_profile = "python_local"
        suggestion_reason = "Python repo without enough reproducible environment evidence; favor localized checks."
    else:
        suggested_profile = "minimal"
        suggestion_reason = "No clear Python repo structure detected."
    return ValidationContext(
        is_python_repo=is_python_repo,
        has_tests=has_tests,
        has_pyproject=has_pyproject,
        has_requirements=has_requirements,
        has_setup_cfg=has_setup_cfg,
        has_src_dir=has_src_dir,
        has_main_py=has_main_py,
        has_dot_venv=has_dot_venv,
        has_uv_lock=has_uv_lock,
        python_runner=runner,
        python_runner_source=runner_source,
        runner_reproducible=runner_reproducible,
        runner_usable=runner_usable,
        pytest_available=pytest_available,
        pytest_command=pytest_command,
        compile_command=compile_command,
        repo_notes=repo_notes,
        suggested_profile=suggested_profile,
        suggestion_reason=suggestion_reason,
        promotion_reasons=promotion_reasons,
        promotion_blockers=promotion_blockers,
    )


def resolve_validation_plan(repo: Path, args) -> ValidationPlan:
    context = detect_validation_context(repo)
    requested = (getattr(args, "validation_profile", "") or "auto").strip().lower()
    if requested not in {"auto", "minimal", "python_local", "python_repo", "strict"}:
        raise SystemExit(f"validation_profile inválido: {requested}.")
    if requested == "auto":
        profile = context.suggested_profile
        source = "auto"
    else:
        profile = requested
        source = "explicit"
    promotion_decision = "promoted" if profile == "python_repo" and context.suggested_profile == "python_repo" else "not_promoted"
    if requested == "strict" and context.suggested_profile == "python_repo":
        promotion_decision = "promoted"
    if requested in {"python_repo", "strict"} and context.suggested_profile != "python_repo":
        promotion_decision = "forced"

    explicit_test_cmd = str(getattr(args, "test_cmd", "") or "").strip()
    explicit_lint_cmd = str(getattr(args, "lint_cmd", "") or "").strip() or None
    test_command = explicit_test_cmd
    if not test_command:
        if profile in {"python_repo", "strict"} and context.pytest_command:
            test_command = context.pytest_command
        elif profile in {"python_local", "python_repo", "strict"} and context.compile_command:
            test_command = context.compile_command

    semantic_enabled = bool(getattr(args, "semantic_validation", False)) or profile in {"python_local", "python_repo", "strict"}
    semantic_mode = getattr(args, "semantic_validation_mode", "auto") or "auto"
    semantic_strict = bool(getattr(args, "semantic_validation_strict", False)) or profile in {"python_local", "python_repo", "strict"}
    semantic_timeout = int(getattr(args, "semantic_validation_timeout", 60) or 60)
    smoke_imports = profile in {"python_repo", "strict"} and context.runner_reproducible and context.runner_usable and bool(context.python_runner)
    smoke_imports_strict = profile == "strict"
    checks = [
        {"name": "test_command", "level": 1, "blocking": True, "status": "pending", "planned": bool(test_command)},
        {"name": "semantic_ast", "level": 2, "blocking": semantic_strict, "status": "pending" if semantic_enabled else "skipped", "planned": semantic_enabled},
        {"name": "optional_python_tool", "level": 3, "blocking": profile == "strict", "status": "pending" if semantic_enabled else "skipped", "planned": semantic_enabled},
        {"name": "smoke_imports", "level": 4, "blocking": smoke_imports_strict, "status": "pending" if smoke_imports else "skipped", "planned": smoke_imports},
    ]
    return ValidationPlan(
        profile=profile,
        source=source,
        suggested_profile=context.suggested_profile,
        suggestion_reason=context.suggestion_reason,
        promotion_decision=promotion_decision,
        promotion_reasons=context.promotion_reasons,
        promotion_blockers=context.promotion_blockers,
        test_command=test_command,
        lint_command=explicit_lint_cmd,
        semantic_validation=semantic_enabled,
        semantic_validation_mode=semantic_mode,
        semantic_validation_strict=semantic_strict,
        semantic_validation_timeout=semantic_timeout,
        smoke_imports=smoke_imports,
        smoke_imports_strict=smoke_imports_strict,
        smoke_import_runner=context.python_runner if smoke_imports else None,
        checks=checks,
        context=context,
    )


def build_reasoner_prompt(
    repo: Path,
    goals_text: str,
    extra_context_files: List[str],
    settings: PlanSettings,
    validation_commands: List[str],
) -> str:
    tree = repo_tree(repo, max_lines=settings.max_tree_lines)
    selected = select_plan_context_files(repo, extra_context_files, settings.max_files)
    key_blobs: List[str] = []
    for path in selected:
        key_blobs.append(
            f"\n\n### FILE: {path}\n```text\n{head_file(repo, path, max_chars=settings.max_file_chars)}\n```"
        )
    validation_block = "\n".join(f"- {cmd}" for cmd in validation_commands) or "- [no clear validation command detected]"

    if settings.mode == "fast":
        return f"""\
You are a senior engineer preparing a short implementation plan for a repository.
Output ONLY Markdown for plan.md. No preamble. No code fences around the whole answer.

Use EXACTLY this structure:
{FAST_PLAN_STRUCTURE}

STRICT RULES:
- Propose at most {settings.max_changes} changes.
- Keep every section concise.
- No style-only refactors.
- Only propose changes that are implementable with the files and dependencies already present in the repo context.
- Do not rely on adding new third-party dependencies in fast mode.
- Summary, Assumptions, Non-goals: max 3 bullets each.
- Commands to validate: max 4 exact commands.
- Use only commands from the allowed validation list below.
- Rationale: one sentence per change.
- Acceptance criteria: max 3 bullets per change.
- Tests: max 2 bullets per change.

INPUTS:

## PROJECT_GOALS.md
{truncate(goals_text, 2500)}

## REPO FILE LIST (truncated)
{tree}
{''.join(key_blobs)}

## ALLOWED VALIDATION COMMANDS
{validation_block}

Now output the final Markdown for plan.md only.
"""

    return f"""\
You are a meticulous senior engineer. You will review a repository and produce a single Markdown document called plan.md
following EXACTLY the required structure below. Do not include anything before the Markdown. Do not wrap it in code fences.

REQUIRED STRUCTURE (copy it and fill it):
{PLAN_TEMPLATE}

CONSTRAINTS:
- Keep changes realistic and incremental.
- Prioritize: correctness, tests, maintainability, clarity.
- Respect the goals in PROJECT_GOALS.md.
- Do NOT propose irrelevant refactors or style-only churn.
- In "Commands to validate", include exact commands that match the repo tooling you see.
- Use only commands from the allowed validation list below.
- Propose at most {settings.max_changes} changes.

INPUTS:

## PROJECT_GOALS.md
{truncate(goals_text, settings.max_file_chars)}

## REPO FILE LIST (git ls-files)
{tree}
{''.join(key_blobs)}

## ALLOWED VALIDATION COMMANDS
{validation_block}

Now output the final Markdown for plan.md only.
"""


def default_aider_instruction(
    plan_md: str,
    *,
    selected_plan_md: str = "",
    allowed_files: Optional[List[str]] = None,
    validation_commands: Optional[List[str]] = None,
    plan_mode: str = "balanced",
) -> str:
    scope_lines = []
    if selected_plan_md.strip():
        scope_lines.append("Implement ONLY the selected change subset below.")
    if allowed_files:
        scope_lines.append("Do not edit files outside this allowlist:")
        scope_lines.extend(f"- {path}" for path in allowed_files)
    if validation_commands:
        scope_lines.append("Use these validation commands if you need to verify the change:")
        scope_lines.extend(f"- {cmd}" for cmd in validation_commands[:4])
    if plan_mode == "fast":
        scope_lines.append("In fast mode, stop after the first cohesive implementation that satisfies the selected change.")
    scope_block = "\n".join(scope_lines).strip()
    selected_block = selected_plan_md.strip() or plan_md.strip()
    return f"""\
You are Aider acting as an engineer implementing a prepared change plan.

You MUST follow this plan strictly:
- Read AI/plan.md.
- Read the selected scope below and treat it as the execution boundary.
- Implement only high-confidence changes that are directly supported by the current repo.
- Prefer the smallest useful change set over a broad speculative rewrite.
- Implement the selected changes in order.
- Update or add tests only if the selected change truly requires it.
- Do not touch README or docs unless the selected scope explicitly requires it.
- Do not introduce unrelated refactors.
- Do not invent files, tests, scripts or commands that are not already justified by the repo.
- Do not add new dependencies unless the repo already declares them or the selected scope explicitly requires them.
- Keep commits small and meaningful (auto-commits is enabled).

After completing changes, ensure the repo is left in a runnable state.

EXECUTION SCOPE:
{scope_block or "- Keep scope minimal and explicit."}

SELECTED PLAN:
{selected_block}
"""


def build_report_prompt(
    goals_text: str,
    plan_text: str,
    test_report_md: str,
    git_diff: str,
    run_metadata: str = "",
) -> str:
    return f"""\
You are a strict reviewer. Produce a Markdown report called final.md.
Do not wrap output in code fences.

The report must include:
1) Executive summary (what changed, why)
   - State clearly if the run was fully successful or only partially successful.
2) Goals coverage (map each goal -> evidence from changes/tests)
3) Test results interpretation (pass/fail, what it means)
4) Risks and regressions
5) Next steps (concrete)
6) Stage status summary (snapshot/plan/apply/test/report from the metadata)

INPUTS:

## PROJECT_GOALS.md
{goals_text}

## AI/plan.md
{plan_text}

## AI/test-report.md
{test_report_md}

## AI/run-metadata.json
{run_metadata or "[Missing run metadata]"}

## git diff (against base)
{git_diff}

Now output final.md only.
"""


def build_fallback_final_report(
    metadata: Dict[str, Any],
    plan_text: str = "",
    test_report_md: str = "",
) -> str:
    stage_lines = []
    for stage in metadata.get("stages", []):
        stage_lines.append(
            f"- {stage['name']}: {stage.get('status', 'unknown')} "
            f"({format_duration(stage.get('duration_seconds'))})"
        )
    apply_details = next(
        (stage.get("details", {}) for stage in metadata.get("stages", []) if stage.get("name") == "apply"),
        {},
    )
    changed_files = apply_details.get("changed_files") or []
    changed_file_lines = [f"- `{path}`" for path in changed_files] or ["- No committed changes were recorded."]
    error = metadata.get("error") or {}
    error_message = error.get("message", "No recorded error.")
    plan_summary = []
    for change in parse_plan_changes(plan_text)[:3]:
        plan_summary.append(f"- Change {change.index}: {change.title}")
    if not plan_summary:
        plan_summary = ["- No parsed plan changes available."]
    test_status = next(
        (stage.get("status") for stage in metadata.get("stages", []) if stage.get("name") == "test"),
        "not_run",
    )
    return "\n".join(
        [
            "# Final Report",
            "",
            "## Executive summary",
            f"- Overall status: **{metadata.get('status', 'unknown')}**.",
            f"- Error: {error_message}",
            f"- Test stage status: **{test_status}**.",
            "",
            "## Goals coverage",
            "- This fallback report is based on recorded stage metadata, not a successful reviewer model summary.",
            "- Review AI/plan.md and git history for full intent vs. execution.",
            "",
            "## Planned changes",
            *plan_summary,
            "",
            "## Applied changes",
            *changed_file_lines,
            "",
            "## Test results interpretation",
            f"- {test_status}: see AI/test-report.md for details." if test_report_md.strip() else "- No test report was produced.",
            "",
            "## Risks and regressions",
            "- This run did not complete cleanly, so the repo may be partially modified or unchanged.",
            "- Any missing report sections should be treated as unknown, not successful.",
            "",
            "## Next steps",
            "- Inspect AI/* artifacts for the failed stage and the exact command output.",
            "- Narrow scope further or adjust model/timeouts before rerunning.",
            "",
            "## Stage status summary",
            *stage_lines,
            "",
        ]
    )


def build_grounded_final_report(
    metadata: Dict[str, Any],
    plan_text: str,
    test_report_md: str,
    changed_files: List[str],
    git_diff: str,
) -> str:
    stage_status = {stage["name"]: stage.get("status", "unknown") for stage in metadata.get("stages", [])}
    stage_lines = [
        f"- {stage['name']}: {stage.get('status', 'unknown')} ({format_duration(stage.get('duration_seconds'))})"
        for stage in metadata.get("stages", [])
    ]
    plan_changes = parse_plan_changes(plan_text)
    plan_lines = [f"- Change {change.index}: {change.title}" for change in plan_changes] or ["- No parsed plan changes."]
    changed_file_lines = [f"- `{path}`" for path in changed_files] or ["- No committed files detected."]
    overall_status = metadata.get("status", "unknown")
    tests_status = stage_status.get("test", "not_run")
    fully_successful = overall_status == "ok" and tests_status == "ok"
    apply_details = next(
        (stage.get("details", {}) for stage in metadata.get("stages", []) if stage.get("name") == "apply"),
        {},
    )
    selected_titles = apply_details.get("selected_change_titles") or []
    selected_lines = [f"- {title}" for title in selected_titles] or ["- No selected change titles recorded."]
    test_summary_line = "- No test report was produced."
    semantic_summary_line = "- No semantic validation evidence."
    validation_profile_line = "- Validation profile not recorded."
    checks_line = "- Validation checks not recorded."
    promotion_line = "- Promotion decision not recorded."
    runner_line = "- Runner not recorded."
    semantic_alert = False
    if test_report_md.strip():
        match = re.search(r"- stage_status: \*\*(.+?)\*\*", test_report_md)
        semantic_match = re.search(r"- semantic_validation_status: \*\*(.+?)\*\*", test_report_md)
        profile_match = re.search(r"- validation_profile: \*\*(.+?)\*\*", test_report_md)
        promotion_match = re.search(r"- promotion_decision: \*\*(.+?)\*\*", test_report_md)
        runner_match = re.search(r"- runner: \*\*(.+?)\*\*", test_report_md)
        if match:
            test_summary_line = f"- Test report status: **{match.group(1)}**."
        else:
            test_summary_line = "- Test report exists in AI/test-report.md."
        if semantic_match:
            semantic_status = semantic_match.group(1)
            semantic_summary_line = f"- Semantic validation status: **{semantic_status}**."
            semantic_alert = semantic_status in {"warning", "failed"}
        if profile_match:
            validation_profile_line = f"- Validation profile used: **{profile_match.group(1)}**."
        if promotion_match:
            promotion_line = f"- Promotion decision: **{promotion_match.group(1)}**."
        if runner_match:
            runner_line = f"- Validation runner: **{runner_match.group(1)}**."
        checks = re.findall(r"- L(\d+) `([^`]+)`: \*\*(.+?)\*\* blocking=(True|False)", test_report_md)
        if checks:
            checks_line = "- Validation checks: " + "; ".join(f"L{level} {name}={status}" for level, name, status, _ in checks[:6]) + "."
    diff_excerpt = truncate(git_diff.strip(), 4000) if git_diff.strip() else "[No git diff recorded]"
    return "\n".join(
        [
            "# Final Report",
            "",
            "## Executive Summary",
            f"- Overall status: **{overall_status}**.",
            f"- Fully successful: **{'yes' if fully_successful else 'no'}**.",
            f"- Selected plan scope: {', '.join(selected_titles) if selected_titles else 'not recorded'}.",
            f"- Committed files: {', '.join(changed_files) if changed_files else 'none'}.",
            "",
            "## Goals Coverage",
            "- Planned changes:",
            *plan_lines,
            "- Selected changes sent to apply:",
            *selected_lines,
            "- Actual committed files:",
            *changed_file_lines,
            "",
            "## Test Results Interpretation",
            test_summary_line,
            semantic_summary_line,
            validation_profile_line,
            promotion_line,
            runner_line,
            checks_line,
            "- Current validation only proves the repo still compiles under `python3 -m compileall src main.py`.",
            "",
            "## Risks and Regressions",
            "- This report is grounded on metadata and git diff to avoid hallucinated success claims.",
            "- A green compile check is weaker than behavioral tests; functional regressions may still exist.",
            "- Semantic validation reported suspicious Python issues." if semantic_alert else "- No semantic validation alerts were recorded.",
            "- Review the actual diff before treating the change as complete.",
            "",
            "## Next Steps",
            "- Inspect the committed diff and decide whether the applied change is functionally sufficient.",
            "- Strengthen `test_cmd` when the target repo has installable dependencies or sample fixtures.",
            "- If the first plan change remains too ambitious, keep `apply` limited to one scoped change per run.",
            "",
            "## Stage Status Summary",
            *stage_lines,
            "",
            "## Git Diff Excerpt",
            "```diff",
            diff_excerpt,
            "```",
            "",
        ]
    )


def project_reported_metadata(metadata: Dict[str, Any], overall_status: Optional[str] = None) -> Dict[str, Any]:
    projected = copy.deepcopy(metadata)
    if overall_status:
        projected["status"] = overall_status
    for stage in projected.get("stages", []):
        if stage.get("name") == "report":
            stage["status"] = "ok"
    return projected


def run_plan_generation(
    repo: Path,
    ai_dir: Path,
    goals_text: str,
    args,
) -> Tuple[str, Dict[str, Any]]:
    attempts = build_plan_attempt_specs(args)
    validation_commands = detect_validation_commands(repo, args)
    clear_artifacts(
        ai_dir,
        [
            "plan.md",
            "plan-prompt.txt",
            "plan-raw.txt",
            "plan-error.md",
            "plan-attempts.json",
        ],
    )
    attempt_records: List[Dict[str, Any]] = []
    last_exc: Optional[BaseException] = None

    for attempt in attempts:
        settings = resolve_plan_settings(args, mode_override=attempt.mode)
        prompt = build_reasoner_prompt(repo, goals_text, args.extra_context, settings, validation_commands)
        attempt_prefix = f"plan-attempt-{attempt.index}"
        save_model_artifacts(ai_dir, attempt_prefix, prompt)
        safe_write(ai_dir / "plan-prompt.txt", prompt)
        print(
            "plan: attempt "
            f"{attempt.index}/{len(attempts)} "
            f"label={attempt.label} mode={attempt.mode} reasoner={attempt.reasoner}"
        )

        record: Dict[str, Any] = {
            "attempt": attempt.index,
            "label": attempt.label,
            "mode": attempt.mode,
            "reasoner": attempt.reasoner,
            "timeout_seconds": attempt.timeout,
            "max_tree_lines": settings.max_tree_lines,
            "max_files": settings.max_files,
            "max_file_chars": settings.max_file_chars,
            "max_changes": settings.max_changes,
            "prompt_path": str(ai_dir / f"{attempt_prefix}-prompt.txt"),
            "raw_path": str(ai_dir / f"{attempt_prefix}-raw.txt"),
            "status": "running",
        }

        try:
            raw = call_model(parse_model_spec(attempt.reasoner), prompt, cwd=repo, timeout=attempt.timeout)
            save_model_artifacts(ai_dir, attempt_prefix, prompt, raw)
            save_model_artifacts(ai_dir, "plan", prompt, raw)
            plan_md = normalize_model_markdown(raw, "# Repo Review Plan")
            plan_md = sanitize_plan_commands(plan_md, validation_commands)
            safe_write(ai_dir / "plan.md", plan_md)
            record["status"] = "ok"
            record["plan_path"] = str(ai_dir / "plan.md")
            attempt_records.append(record)
            safe_write(ai_dir / "plan-attempts.json", json.dumps(attempt_records, indent=2, ensure_ascii=False))
            return plan_md, {
                "attempts": attempt_records,
                "selected_attempt": attempt.index,
                "selected_mode": attempt.mode,
                "selected_reasoner": attempt.reasoner,
                "reasoner_timeout": attempt.timeout,
            }
        except BaseException as exc:
            last_exc = exc
            raw_text = ""
            status = "failed"
            if isinstance(exc, ExecutionFailure):
                raw_text = exc.stdout
                status = exc.status
            elif isinstance(exc, subprocess.TimeoutExpired):
                raw_text = to_text(exc.stdout)
                status = "timeout"

            if raw_text:
                safe_write(ai_dir / f"{attempt_prefix}-raw.txt", raw_text)
                safe_write(ai_dir / "plan-raw.txt", raw_text)

            record["status"] = status
            record["error"] = str(exc)
            attempt_records.append(record)
            safe_write(ai_dir / "plan-attempts.json", json.dumps(attempt_records, indent=2, ensure_ascii=False))

            retryable = status == "timeout" and args.plan_retry_on_timeout
            if retryable and attempt.index < len(attempts):
                print(f"plan: attempt {attempt.index} timed out; degrading strategy.")
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("No plan attempts were executed.")


# -----------------------------
# Validation / preflight
# -----------------------------

def preflight(args, repo: Path) -> None:
    ensure_git_repo(repo)

    if not command_exists("git"):
        raise SystemExit("No se encontró 'git' en PATH.")

    if args.cmd in {"plan", "report", "run"} and args.reasoner.strip():
        if args.cmd in {"plan", "run"}:
            resolve_plan_settings(args)
        validate_model_available(parse_model_spec(args.reasoner))
        if args.cmd in {"plan", "run"} and args.plan_fallback_reasoner.strip():
            validate_model_available(parse_model_spec(args.plan_fallback_reasoner))

    if args.cmd in {"apply", "run"} and args.aider_model.strip():
        aider_model = parse_model_spec(args.aider_model)
        if aider_model.kind != "ollama":
            raise SystemExit("aider_model debe usar formato ollama:<modelo>.")
        validate_model_available(aider_model)

    if args.cmd in {"apply", "run"} and not command_exists("aider"):
        raise SystemExit("No se encontró 'aider' en PATH.")

    if args.cmd in {"plan", "report", "run"}:
        goals_path = resolve_goals_path(repo, args.goals)
        if not goals_path.exists():
            raise SystemExit(f"No existe el archivo de goals: {goals_path}")

    if args.cmd in {"test", "run"}:
        validation_plan = resolve_validation_plan(repo, args)
        require_nonempty(validation_plan.test_command, "validation test command", "Config: validation_profile/test_cmd")
        if not looks_like_shell_command(validation_plan.test_command):
            first = shlex.split(validation_plan.test_command)[0]
            if not command_exists(first):
                raise SystemExit(f"No se encontró el ejecutable del test_cmd: {first}")

        if validation_plan.lint_command and not looks_like_shell_command(validation_plan.lint_command):
            first = shlex.split(validation_plan.lint_command)[0]
            if not command_exists(first):
                raise SystemExit(f"No se encontró el ejecutable del lint_cmd: {first}")

    if not args.allow_dirty_repo and args.cmd in {"apply", "run"}:
        dirty = git_status_porcelain(repo)
        if dirty:
            raise SystemExit(
                "El repo objetivo tiene cambios sin commit.\n"
                "Haz commit/stash primero o activa --allow-dirty-repo."
            )


# -----------------------------
# Test report
# -----------------------------

def write_test_report(
    repo: Path,
    ai_dir: Path,
    lint_cmd: Optional[str],
    test_cmd: str,
    lint_required: bool = False,
    timeout: int = 1800,
    changed_files: Optional[List[str]] = None,
    semantic_validation: bool = False,
    semantic_validation_mode: str = "auto",
    semantic_validation_strict: bool = False,
    semantic_validation_timeout: int = 60,
    validation_plan: Optional[ValidationPlan] = None,
) -> Tuple[bool, str, Dict[str, Any]]:
    """
    Run lint and tests. Save a Markdown report and JSON summary.
    Returns (passed, report_md).
    """
    entries = []
    summary = {
        "validation_profile": None,
        "lint": None,
        "tests": None,
        "lint_required": lint_required,
        "passed": False,
        "timestamp": now_stamp(),
        "semantic_validation": {
            "enabled": semantic_validation,
            "status": "skipped",
            "issues": [],
            "warnings": [],
            "files_checked": [],
            "tools": [],
        },
        "checks": [],
    }
    if validation_plan is None:
        validation_context = detect_validation_context(repo)
        validation_plan = ValidationPlan(
            profile="manual",
            source="manual",
            suggested_profile=validation_context.suggested_profile,
            suggestion_reason=validation_context.suggestion_reason,
            promotion_decision="manual",
            promotion_reasons=validation_context.promotion_reasons,
            promotion_blockers=validation_context.promotion_blockers,
            test_command=test_cmd,
            lint_command=lint_cmd,
            semantic_validation=semantic_validation,
            semantic_validation_mode=semantic_validation_mode,
            semantic_validation_strict=semantic_validation_strict,
            semantic_validation_timeout=semantic_validation_timeout,
            smoke_imports=False,
            smoke_imports_strict=False,
            smoke_import_runner=None,
            checks=[],
            context=validation_context,
        )
    safe_write(ai_dir / "validation-execution-plan.json", json.dumps(validation_plan_to_dict(validation_plan), indent=2, ensure_ascii=False))
    lint_cmd = validation_plan.lint_command
    test_cmd = validation_plan.test_command
    summary["validation_profile"] = validation_plan_to_dict(validation_plan)

    def run_and_capture(label: str, cmd: str) -> dict:
        use_shell = looks_like_shell_command(cmd)
        try:
            rc, out, err = run_cmd(cmd, cwd=repo, capture=True, use_shell=use_shell, timeout=timeout)
            status = "ok" if rc == 0 else "failed"
            return {
                "label": label,
                "cmd": cmd,
                "returncode": rc,
                "stdout": out,
                "stderr": err,
                "use_shell": use_shell,
                "status": status,
                "timeout_seconds": None,
            }
        except subprocess.TimeoutExpired as exc:
            return {
                "label": label,
                "cmd": cmd,
                "returncode": None,
                "stdout": to_text(exc.stdout),
                "stderr": to_text(exc.stderr),
                "use_shell": use_shell,
                "status": "timeout",
                "timeout_seconds": timeout,
            }

    lint_ok = True
    if lint_cmd:
        lint_res = run_and_capture("lint", lint_cmd)
        lint_ok = lint_res["returncode"] == 0
        summary["lint"] = {
            "returncode": lint_res["returncode"],
            "status": lint_res["status"],
            "timeout_seconds": lint_res["timeout_seconds"],
        }
        entries.append(lint_res)
        summary["checks"].append({"name": "lint", "level": 1, "status": lint_res["status"], "blocking": lint_required})

    test_res = run_and_capture("tests", test_cmd)
    test_ok = test_res["returncode"] == 0
    summary["tests"] = {
        "returncode": test_res["returncode"],
        "status": test_res["status"],
        "timeout_seconds": test_res["timeout_seconds"],
    }
    entries.append(test_res)
    summary["checks"].append({"name": "test_command", "level": 1, "status": test_res["status"], "blocking": True, "command": test_cmd})

    passed = test_ok and (lint_ok or not lint_required)
    summary["passed"] = passed
    semantic_summary = run_semantic_validation(
        repo,
        changed_files or [],
        enabled=validation_plan.semantic_validation,
        mode=validation_plan.semantic_validation_mode,
        strict=validation_plan.semantic_validation_strict,
        timeout=validation_plan.semantic_validation_timeout,
    )
    summary["semantic_validation"] = semantic_summary
    summary["checks"].append(
        {
            "name": "semantic_ast",
            "level": 2,
            "status": semantic_summary["status"],
            "blocking": validation_plan.semantic_validation_strict,
        }
    )
    tool_status = semantic_summary["tools"][0]["status"] if semantic_summary["tools"] else "skipped"
    summary["checks"].append(
        {
            "name": "optional_python_tool",
            "level": 3,
            "status": tool_status,
            "blocking": validation_plan.profile == "strict",
        }
    )
    smoke_summary = run_python_smoke_imports(
        repo,
        changed_files or [],
        validation_plan.smoke_import_runner,
        validation_plan.semantic_validation_timeout,
    ) if validation_plan.smoke_imports else {"status": "skipped", "issues": [], "warnings": ["Smoke imports omitted."], "modules": []}
    summary["smoke_imports"] = smoke_summary
    summary["checks"].append(
        {
            "name": "smoke_imports",
            "level": 4,
            "status": smoke_summary["status"],
            "blocking": validation_plan.smoke_imports_strict,
        }
    )
    if any(e["status"] == "timeout" for e in entries):
        summary["status"] = "timeout"
    elif semantic_summary["status"] == "failed":
        summary["status"] = "failed"
        summary["passed"] = False
    elif smoke_summary["status"] == "failed":
        summary["status"] = "failed" if validation_plan.smoke_imports_strict else "warning"
        if validation_plan.smoke_imports_strict:
            summary["passed"] = False
    elif semantic_summary["status"] == "warning":
        summary["status"] = "warning" if passed else "failed"
    elif smoke_summary["status"] == "warning":
        summary["status"] = "warning" if passed else "failed"
    else:
        summary["status"] = "ok" if passed else "failed"
    passed = summary["passed"]

    md_parts = [f"# Test Report\n\nTimestamp: {summary['timestamp']}\n"]
    md_parts.append(f"- lint_required: **{lint_required}**\n")
    md_parts.append(f"- overall_passed: **{summary['passed']}**\n")
    md_parts.append(f"- stage_status: **{summary['status']}**\n")
    md_parts.append(f"- semantic_validation_status: **{semantic_summary['status']}**\n")
    md_parts.append(f"- validation_profile: **{validation_plan.profile}** ({validation_plan.source})\n")
    md_parts.append(f"- suggested_profile: **{validation_plan.suggested_profile}**\n")
    md_parts.append(f"- profile_reason: {validation_plan.suggestion_reason}\n")
    md_parts.append(f"- promotion_decision: **{validation_plan.promotion_decision}**\n")
    md_parts.append(f"- runner: **{validation_plan.context.python_runner or 'none'}** ({validation_plan.context.python_runner_source})\n")

    for e in entries:
        md_parts.append(f"\n## {e['label'].title()}\n")
        md_parts.append(f"Command:\n```bash\n{e['cmd']}\n```\n")
        md_parts.append(f"Used shell: **{e['use_shell']}**\n")
        md_parts.append(f"Status: **{e['status']}**\n")
        md_parts.append(f"Return code: **{e['returncode']}**\n")
        if e["timeout_seconds"]:
            md_parts.append(f"Timeout seconds: **{e['timeout_seconds']}**\n")
        md_parts.append("STDOUT:\n```text\n" + truncate(e["stdout"], 15000) + "\n```\n")
        md_parts.append("STDERR:\n```text\n" + truncate(e["stderr"], 15000) + "\n```\n")

    md_parts.append("\n## Semantic Validation\n")
    md_parts.append(f"- enabled: **{semantic_summary['enabled']}**\n")
    md_parts.append(f"- status: **{semantic_summary['status']}**\n")
    if semantic_summary["files_checked"]:
        md_parts.append("- files_checked:\n")
        md_parts.extend(f"  - `{path}`\n" for path in semantic_summary["files_checked"])
    if semantic_summary["issues"]:
        md_parts.append("- issues:\n")
        for issue in semantic_summary["issues"]:
            location = f" ({issue['file']}:{issue.get('line')})" if issue.get("file") else ""
            md_parts.append(f"  - [{issue['kind']}] {issue['message']}{location}\n")
    if semantic_summary["warnings"]:
        md_parts.append("- warnings:\n")
        for warning in semantic_summary["warnings"]:
            md_parts.append(f"  - {warning}\n")
    if semantic_summary["tools"]:
        md_parts.append("- tools:\n")
        for tool in semantic_summary["tools"]:
            name = tool.get("tool") or "none"
            md_parts.append(f"  - `{name}`: {tool.get('status', 'unknown')}\n")

    md_parts.append("\n## Smoke Imports\n")
    md_parts.append(f"- status: **{smoke_summary['status']}**\n")
    if smoke_summary.get("modules"):
        md_parts.append("- modules:\n")
        for module in smoke_summary["modules"]:
            md_parts.append(f"  - `{module}`\n")
    if smoke_summary.get("issues"):
        md_parts.append("- issues:\n")
        for issue in smoke_summary["issues"]:
            md_parts.append(f"  - [{issue['kind']}] {issue['module']}: {issue['message']}\n")
    if smoke_summary.get("warnings"):
        md_parts.append("- warnings:\n")
        for warning in smoke_summary["warnings"]:
            md_parts.append(f"  - {warning}\n")

    md_parts.append("\n## Validation Checks\n")
    for check in summary["checks"]:
        md_parts.append(
            f"- L{check['level']} `{check['name']}`: **{check['status']}**"
            f" blocking={check['blocking']}\n"
        )

    md_parts.append("\n## Validation Plan\n")
    if validation_plan.promotion_reasons:
        md_parts.append("- promotion_reasons:\n")
        for reason in validation_plan.promotion_reasons:
            md_parts.append(f"  - {reason}\n")
    if validation_plan.promotion_blockers:
        md_parts.append("- promotion_blockers:\n")
        for blocker in validation_plan.promotion_blockers:
            md_parts.append(f"  - {blocker}\n")
    if validation_plan.context.repo_notes:
        md_parts.append("- repo_notes:\n")
        for note in validation_plan.context.repo_notes:
            md_parts.append(f"  - {note}\n")

    report_md = "\n".join(md_parts)
    safe_write(ai_dir / "test-report.md", report_md)
    safe_write(ai_dir / "test-report.json", json.dumps(summary, indent=2, ensure_ascii=False))

    return passed, report_md, summary


# -----------------------------
# Commands
# -----------------------------

def cmd_snapshot(args) -> None:
    repo, ai_dir, recorder = init_run_context(args)
    preflight(args, repo)

    if not args.zip:
        print("snapshot: --zip no está activado; no se creó zip.")
        recorder.mark_stage_skipped("snapshot", "zip_disabled")
        recorder.complete_run("ok", {"snapshot_created": False})
        return

    with record_stage(recorder, "snapshot") as details:
        out_zip = Path(args.out).expanduser() if args.out else ai_dir / "snapshots" / f"{now_stamp()}.zip"
        if not out_zip.is_absolute():
            out_zip = repo / out_zip

        git_archive_zip(repo, out_zip, ref=args.ref)
        details["artifact"] = str(out_zip)
        print(f"snapshot: wrote {out_zip}")
    recorder.complete_run("ok", {"snapshot_created": True})


def cmd_plan(args) -> None:
    repo, ai_dir, recorder = init_run_context(args)
    preflight(args, repo)

    require_nonempty(args.reasoner, "reasoner", "Config: reasoner: 'ollama:deepseek-r1'")
    goals_path = resolve_goals_path(repo, args.goals)
    goals_text = read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n")

    reasoner = parse_model_spec(args.reasoner)
    plan_out = Path(args.plan_out).expanduser() if args.plan_out else ai_dir / "plan.md"
    if not plan_out.is_absolute():
        plan_out = repo / plan_out

    try:
        with record_stage(recorder, "plan") as details:
            plan_md, plan_meta = run_plan_generation(repo, ai_dir, goals_text, args)
            if plan_out != ai_dir / "plan.md":
                safe_write(plan_out, plan_md)
            details["artifact"] = str(plan_out)
            details["prompt_path"] = str(ai_dir / "plan-prompt.txt")
            details["raw_path"] = str(ai_dir / "plan-raw.txt")
            details["sanitized_path"] = str(plan_out)
            details.update(plan_meta)
            print(f"plan: wrote {plan_out}")
    except BaseException as exc:
        error_path = write_stage_error(
            ai_dir,
            "plan",
            exc,
            {
                "reasoner_timeout": args.reasoner_timeout,
                "plan_mode": args.plan_mode,
                "plan_fallback_reasoner": args.plan_fallback_reasoner,
                "plan_fallback_timeout": effective_plan_fallback_timeout(args),
                "plan_attempts_path": str(ai_dir / "plan-attempts.json"),
            },
        )
        recorder.fail_run(str(exc), "timeout" if isinstance(exc, ExecutionFailure) and exc.status == "timeout" else "failed", {"stage": "plan", "error_path": str(error_path)})
        print(f"plan: failed. See {error_path}")
        raise
    recorder.complete_run("ok", {"plan_path": str(plan_out)})


def cmd_apply(args) -> None:
    repo, ai_dir, recorder = init_run_context(args)
    preflight(args, repo)

    require_nonempty(args.aider_model, "aider_model", "Config: aider_model: 'ollama:qwen3-coder:14b'")

    plan_path = ai_dir / "plan.md"
    plan_text = read_text(plan_path, default="")
    if not plan_text.strip():
        raise SystemExit(f"apply: falta o está vacío {plan_path}. Ejecuta `plan` primero.")

    try:
        with record_stage(recorder, "apply") as details:
            clear_artifacts(ai_dir, ["aider-log.txt", "aider-message.txt", "apply-error.md", "apply-scope.json", "apply-selected-plan.md"])
            base = resolve_base_branch(repo, args.base)
            branch = args.branch.strip() or f"ai/{now_stamp()}-improvements"
            git_checkout(repo, base)
            git_create_branch(repo, branch)
            pre_apply_head = git_head_commit(repo)

            ensure_repo_ready_for_aider(repo)

            apply_scope = resolve_apply_scope(repo, plan_text, args)
            validation_commands = detect_validation_commands(repo, args)
            aider_msg = default_aider_instruction(
                plan_text,
                selected_plan_md=apply_scope.selected_plan_md,
                allowed_files=apply_scope.selected_files,
                validation_commands=validation_commands,
                plan_mode=args.plan_mode,
            )
            safe_write(ai_dir / "aider-message.txt", aider_msg)
            safe_write(ai_dir / "apply-selected-plan.md", apply_scope.selected_plan_md or "[No selected plan subset]\n")
            safe_write(
                ai_dir / "apply-scope.json",
                json.dumps(
                    {
                        "source": apply_scope.source,
                        "max_changes": apply_scope.max_changes,
                        "selected_files": apply_scope.selected_files,
                        "selected_change_titles": [change.title for change in apply_scope.selected_changes],
                        "skipped_change_titles": apply_scope.skipped_change_titles,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            log_path = ai_dir / "aider-log.txt"

            rc, stdout, err = run_aider(
                repo=repo,
                aider_model=args.aider_model,
                message=aider_msg,
                files=apply_scope.selected_files or None,
                log_path=log_path,
                extra_args=args.aider_extra_arg,
                timeout=args.aider_timeout,
            )
            details["base"] = base
            details["branch"] = branch
            details["log_path"] = str(log_path)
            details["aider_timeout"] = args.aider_timeout
            details["apply_scope_path"] = str(ai_dir / "apply-scope.json")
            details["selected_plan_path"] = str(ai_dir / "apply-selected-plan.md")
            details["apply_scope_source"] = apply_scope.source
            details["selected_files"] = apply_scope.selected_files
            details["selected_change_titles"] = [change.title for change in apply_scope.selected_changes]
            details["skipped_change_titles"] = apply_scope.skipped_change_titles
            details["stdout_excerpt"] = truncate(stdout, 2000)
            if rc != 0 or aider_output_has_fatal_error(stdout, err):
                raise ExecutionFailure(
                    f"Aider failed ({rc}).",
                    command=f"aider --model {normalize_aider_model_name(args.aider_model)}",
                    returncode=rc,
                    stdout=stdout,
                    stderr=err,
                    status="failed",
                )
            post_apply_head = git_head_commit(repo)
            changed_files = git_changed_files_between(repo, pre_apply_head, post_apply_head)
            if post_apply_head == pre_apply_head or not changed_files or set(changed_files) <= {".gitignore"}:
                raise ExecutionFailure(
                    "Aider completed without producing a meaningful committed change.",
                    command=f"aider --model {normalize_aider_model_name(args.aider_model)}",
                    stdout=stdout,
                    stderr=err,
                    status="failed",
                )
            if args.apply_enforce_plan_scope and apply_scope.selected_files:
                scope_violations = [
                    path for path in changed_files if path not in apply_scope.selected_files and path != ".gitignore"
                ]
                if scope_violations:
                    raise ExecutionFailure(
                        "Aider changed files outside the selected apply scope.",
                        command=f"aider --model {normalize_aider_model_name(args.aider_model)}",
                        stdout=stdout,
                        stderr=err,
                        status="failed",
                    )
            dep_audit = audit_python_dependency_declarations(repo, changed_files)
            details["dependency_audit"] = dep_audit
            if not dep_audit["ok"]:
                raise ExecutionFailure(
                    "Aider introduced Python imports whose dependencies are not declared in the repo.",
                    command=f"aider --model {normalize_aider_model_name(args.aider_model)}",
                    stdout=stdout,
                    stderr=json.dumps(dep_audit, ensure_ascii=False),
                    status="failed",
                )
            details["changed_files"] = changed_files
            print(f"apply: aider return code = {rc}")

            dirty = git_status_porcelain(repo)
            details["dirty_after_apply"] = bool(dirty)
            if dirty:
                print("apply: el repo tiene cambios sin commit. Revisa si Aider dejó algo sin auto-commit.")
                print(dirty)
    except BaseException as exc:
        error_path = write_stage_error(ai_dir, "apply", exc, {"aider_timeout": args.aider_timeout})
        recorder.fail_run(str(exc), "timeout" if isinstance(exc, ExecutionFailure) and exc.status == "timeout" else "failed", {"stage": "apply", "error_path": str(error_path)})
        print(f"apply: failed. See {error_path}")
        raise
    recorder.complete_run("ok", {"plan_path": str(plan_path)})


def cmd_test(args) -> None:
    repo, ai_dir, recorder = init_run_context(args)
    preflight(args, repo)

    validation_plan = resolve_validation_plan(repo, args)
    changed_files, changed_base_ref = infer_validation_changed_files(repo, args)
    try:
        with record_stage(recorder, "test") as details:
            clear_artifacts(ai_dir, ["test-report.md", "test-report.json", "test-error.md"])
            passed, _, summary = write_test_report(
                repo,
                ai_dir,
                validation_plan.lint_command,
                validation_plan.test_command,
                lint_required=args.lint_required,
                timeout=args.test_timeout,
                changed_files=changed_files,
                semantic_validation=validation_plan.semantic_validation,
                semantic_validation_mode=validation_plan.semantic_validation_mode,
                semantic_validation_strict=validation_plan.semantic_validation_strict,
                semantic_validation_timeout=validation_plan.semantic_validation_timeout,
                validation_plan=validation_plan,
            )
            details["__status"] = summary["status"]
            details["artifact"] = str(ai_dir / "test-report.md")
            details["test_timeout"] = args.test_timeout
            details["changed_files_base_ref"] = changed_base_ref
            details["changed_files"] = changed_files
            details["validation_profile"] = summary["validation_profile"]
            details["summary"] = summary
            print(f"test: passed={passed} (report in {ai_dir / 'test-report.md'})")
    except BaseException as exc:
        error_path = write_stage_error(ai_dir, "test", exc, {"test_timeout": args.test_timeout})
        recorder.fail_run(str(exc), "timeout" if isinstance(exc, ExecutionFailure) and exc.status == "timeout" else "failed", {"stage": "test", "error_path": str(error_path)})
        print(f"test: failed. See {error_path}")
        raise
    recorder.complete_run(summary["status"], {"passed": passed, "test_report": str(ai_dir / "test-report.md"), "validation_profile": validation_plan.profile})
    if not passed:
        raise SystemExit(124 if summary["status"] == "timeout" else 2)


def cmd_report(args) -> None:
    repo, ai_dir, recorder = init_run_context(args)
    preflight(args, repo)

    require_nonempty(args.reasoner, "reasoner", "Config: reasoner: 'ollama:deepseek-r1'")

    goals_path = resolve_goals_path(repo, args.goals)
    goals_text = read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n")

    plan_text = read_text(ai_dir / "plan.md", default="")
    test_report_md = read_text(ai_dir / "test-report.md", default="[Missing test report]")
    run_metadata = read_text(ai_dir / "run-metadata.json", default="[Missing run metadata]")
    if not plan_text.strip():
        raise SystemExit("report: falta AI/plan.md")

    reasoner = parse_model_spec(args.reasoner)
    try:
        with record_stage(recorder, "report") as details:
            clear_artifacts(ai_dir, ["final.md", "report-prompt.txt", "report-raw.txt", "report-model.md", "report-error.md"])
            base_ref = args.base_ref.strip() or infer_base_ref(repo)
            git_diff = collect_git_diff(repo, base_ref)
            changed_files = collect_git_changed_files(repo, base_ref)

            prompt = build_report_prompt(goals_text, plan_text, test_report_md, git_diff, run_metadata)
            save_model_artifacts(ai_dir, "report", prompt)
            raw = call_model(reasoner, prompt, cwd=repo, timeout=effective_report_timeout(args))
            save_model_artifacts(ai_dir, "report", prompt, raw)
            metadata_payload = json.loads(run_metadata) if run_metadata.strip().startswith("{") else recorder.metadata
            final_md = build_grounded_final_report(
                project_reported_metadata(metadata_payload, metadata_payload.get("status")),
                plan_text,
                test_report_md,
                changed_files,
                git_diff,
            )
            safe_write(ai_dir / "report-model.md", normalize_model_markdown(raw, "# Final Report"))

            safe_write(ai_dir / "final.md", final_md)
            details["artifact"] = str(ai_dir / "final.md")
            details["base_ref"] = base_ref
            details["prompt_path"] = str(ai_dir / "report-prompt.txt")
            details["raw_path"] = str(ai_dir / "report-raw.txt")
            details["model_report_path"] = str(ai_dir / "report-model.md")
            details["report_timeout"] = effective_report_timeout(args)
            print(f"report: wrote {ai_dir / 'final.md'} (base_ref={base_ref})")
    except BaseException as exc:
        error_path = write_stage_error(ai_dir, "report", exc, {"report_timeout": effective_report_timeout(args)})
        recorder.fail_run(str(exc), "timeout" if isinstance(exc, ExecutionFailure) and exc.status == "timeout" else "failed", {"stage": "report", "error_path": str(error_path)})
        print(f"report: failed. See {error_path}")
        raise
    recorder.complete_run("ok", {"final_report": str(ai_dir / "final.md")})


def cmd_run(args) -> None:
    repo, ai_dir, recorder = init_run_context(args)
    preflight(args, repo)

    require_nonempty(args.reasoner, "reasoner", "Config: reasoner: 'ollama:deepseek-r1'")
    require_nonempty(args.aider_model, "aider_model", "Config: aider_model: 'ollama:qwen3-coder:14b'")
    require_nonempty(args.test_cmd, "test_cmd", "Config: test_cmd: 'pytest -q'")
    reasoner = parse_model_spec(args.reasoner)

    if args.zip:
        with record_stage(recorder, "snapshot") as details:
            out_zip = ai_dir / "snapshots" / f"{now_stamp()}.zip"
            git_archive_zip(repo, out_zip)
            details["artifact"] = str(out_zip)
            print(f"run: snapshot zip -> {out_zip}")
    else:
        recorder.mark_stage_skipped("snapshot", "zip_disabled")

    goals_path = resolve_goals_path(repo, args.goals)
    goals_text = read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n")
    try:
        with record_stage(recorder, "plan") as details:
            plan_md, plan_meta = run_plan_generation(repo, ai_dir, goals_text, args)
            details["artifact"] = str(ai_dir / "plan.md")
            details["prompt_path"] = str(ai_dir / "plan-prompt.txt")
            details["raw_path"] = str(ai_dir / "plan-raw.txt")
            details.update(plan_meta)
            print(f"run: plan -> {ai_dir / 'plan.md'}")

        validation_plan = resolve_validation_plan(repo, args)
        lint_cmd = validation_plan.lint_command
        test_cmd = validation_plan.test_command
        last_feedback = ""
        final_passed = False
        test_summary: Dict[str, Any] = {"status": "failed"}

        with record_stage(recorder, "apply") as details:
            clear_artifacts(ai_dir, ["aider-log.txt", "aider-message.txt", "apply-error.md", "apply-scope.json", "apply-selected-plan.md", "test-report.md", "test-report.json", "test-error.md", "final.md", "report-prompt.txt", "report-raw.txt", "report-model.md", "report-error.md"])
            base = resolve_base_branch(repo, args.base)
            branch = args.branch.strip() or f"ai/{now_stamp()}-improvements"
            git_checkout(repo, base)
            git_create_branch(repo, branch)
            print(f"run: branch -> {branch} (from {base})")
            pre_apply_head = git_head_commit(repo)

            ensure_repo_ready_for_aider(repo)

            aider_log = ai_dir / "aider-log.txt"
            apply_scope = resolve_apply_scope(repo, plan_md, args)
            validation_commands = detect_validation_commands(repo, args)
            aider_msg = default_aider_instruction(
                plan_md,
                selected_plan_md=apply_scope.selected_plan_md,
                allowed_files=apply_scope.selected_files,
                validation_commands=validation_commands,
                plan_mode=args.plan_mode,
            )
            safe_write(ai_dir / "apply-selected-plan.md", apply_scope.selected_plan_md or "[No selected plan subset]\n")
            safe_write(
                ai_dir / "apply-scope.json",
                json.dumps(
                    {
                        "source": apply_scope.source,
                        "max_changes": apply_scope.max_changes,
                        "selected_files": apply_scope.selected_files,
                        "selected_change_titles": [change.title for change in apply_scope.selected_changes],
                        "skipped_change_titles": apply_scope.skipped_change_titles,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
            )
            details["base"] = base
            details["branch"] = branch
            details["log_path"] = str(aider_log)
            details["aider_timeout"] = args.aider_timeout
            details["apply_scope_path"] = str(ai_dir / "apply-scope.json")
            details["selected_plan_path"] = str(ai_dir / "apply-selected-plan.md")
            details["apply_scope_source"] = apply_scope.source
            details["selected_files"] = apply_scope.selected_files
            details["selected_change_titles"] = [change.title for change in apply_scope.selected_changes]
            details["skipped_change_titles"] = apply_scope.skipped_change_titles

            for i in range(1, args.max_iters + 1):
                msg = aider_msg if i == 1 else (
                    aider_msg
                    + "\n\nAlso fix the following test/lint failures:\n"
                    + truncate(last_feedback, 12000)
                )
                safe_write(ai_dir / "aider-message.txt", msg)
                rc, stdout, err = run_aider(
                    repo=repo,
                    aider_model=args.aider_model,
                    message=msg,
                    files=apply_scope.selected_files or None,
                    log_path=aider_log,
                    extra_args=args.aider_extra_arg,
                    timeout=args.aider_timeout,
                )
                if rc != 0 or aider_output_has_fatal_error(stdout, err):
                    raise ExecutionFailure(
                        f"Aider failed on iteration {i} ({rc}).",
                        command=f"aider --model {normalize_aider_model_name(args.aider_model)}",
                        returncode=rc,
                        stdout=stdout,
                        stderr=err,
                        status="failed",
                    )
                post_apply_head = git_head_commit(repo)
                changed_files = git_changed_files_between(repo, pre_apply_head, post_apply_head)
                if post_apply_head == pre_apply_head or not changed_files or set(changed_files) <= {".gitignore"}:
                    raise ExecutionFailure(
                        "Aider completed without producing a meaningful committed change.",
                        command=f"aider --model {normalize_aider_model_name(args.aider_model)}",
                        stdout=stdout,
                        stderr=err,
                        status="failed",
                    )
                if args.apply_enforce_plan_scope and apply_scope.selected_files:
                    scope_violations = [
                        path for path in changed_files if path not in apply_scope.selected_files and path != ".gitignore"
                    ]
                    if scope_violations:
                        raise ExecutionFailure(
                            "Aider changed files outside the selected apply scope.",
                            command=f"aider --model {normalize_aider_model_name(args.aider_model)}",
                            stdout=stdout,
                            stderr=err,
                            status="failed",
                        )
                dep_audit = audit_python_dependency_declarations(repo, changed_files)
                details["dependency_audit"] = dep_audit
                if not dep_audit["ok"]:
                    raise ExecutionFailure(
                        "Aider introduced Python imports whose dependencies are not declared in the repo.",
                        command=f"aider --model {normalize_aider_model_name(args.aider_model)}",
                        stdout=stdout,
                        stderr=json.dumps(dep_audit, ensure_ascii=False),
                        status="failed",
                    )
                details["last_iteration"] = i
                details["stdout_excerpt"] = truncate(stdout, 2000)
                details["changed_files"] = changed_files

                with record_stage(recorder, "test") as test_details:
                    clear_artifacts(ai_dir, ["test-report.md", "test-report.json", "test-error.md"])
                    passed, test_report_md, test_summary = write_test_report(
                        repo,
                        ai_dir,
                        lint_cmd,
                        test_cmd,
                        lint_required=args.lint_required,
                        timeout=args.test_timeout,
                        changed_files=changed_files,
                        semantic_validation=validation_plan.semantic_validation,
                        semantic_validation_mode=validation_plan.semantic_validation_mode,
                        semantic_validation_strict=validation_plan.semantic_validation_strict,
                        semantic_validation_timeout=validation_plan.semantic_validation_timeout,
                        validation_plan=validation_plan,
                    )
                    test_details["__status"] = test_summary["status"]
                    test_details["artifact"] = str(ai_dir / "test-report.md")
                    test_details["iteration"] = i
                    test_details["test_timeout"] = args.test_timeout
                    test_details["validation_profile"] = test_summary.get("validation_profile")
                    test_details["summary"] = test_summary
                    print(f"run: iter {i} tests passed={passed}")
                if passed:
                    final_passed = True
                    break

                last_feedback = test_report_md

        if not (ai_dir / "test-report.md").exists():
            safe_write(ai_dir / "test-report.md", "[No test report produced]\n")
            recorder.mark_stage_skipped("test", "no_test_report_produced")

        if not final_passed:
            print("run: se agotaron las iteraciones sin conseguir un estado verde completo.")

        with record_stage(recorder, "report") as details:
            clear_artifacts(ai_dir, ["final.md", "report-prompt.txt", "report-raw.txt", "report-model.md", "report-error.md"])
            base_ref = infer_base_ref(repo)
            git_diff = collect_git_diff(repo, base_ref)
            changed_files = collect_git_changed_files(repo, base_ref)
            report_prompt = build_report_prompt(
                goals_text,
                plan_md,
                read_text(ai_dir / "test-report.md"),
                git_diff,
                read_text(ai_dir / "run-metadata.json"),
            )
            save_model_artifacts(ai_dir, "report", report_prompt)
            report_raw = call_model(reasoner, report_prompt, cwd=repo, timeout=effective_report_timeout(args))
            save_model_artifacts(ai_dir, "report", report_prompt, report_raw)
            safe_write(ai_dir / "report-model.md", normalize_model_markdown(report_raw, "# Final Report"))
            projected_status = "ok" if final_passed else test_summary.get("status", "failed")
            final_md = build_grounded_final_report(
                project_reported_metadata(recorder.metadata, projected_status),
                plan_md,
                read_text(ai_dir / "test-report.md"),
                changed_files,
                git_diff,
            )
            safe_write(ai_dir / "final.md", final_md)
            details["artifact"] = str(ai_dir / "final.md")
            details["base_ref"] = base_ref
            details["prompt_path"] = str(ai_dir / "report-prompt.txt")
            details["raw_path"] = str(ai_dir / "report-raw.txt")
            details["model_report_path"] = str(ai_dir / "report-model.md")
            details["report_timeout"] = effective_report_timeout(args)
            print(f"run: final -> {ai_dir / 'final.md'} (base_ref={base_ref})")
    except BaseException as exc:
        failed_stage = next((stage["name"] for stage in reversed(recorder.metadata["stages"]) if stage["status"] in {"running", "failed", "timeout"}), "run")
        error_path = write_stage_error(ai_dir, failed_stage, exc)
        status = "timeout" if isinstance(exc, ExecutionFailure) and exc.status == "timeout" else "failed"
        recorder.fail_run(str(exc), status, {"stage": failed_stage, "error_path": str(error_path)})
        final_md = build_fallback_final_report(
            recorder.metadata,
            read_text(ai_dir / "plan.md"),
            read_text(ai_dir / "test-report.md"),
        )
        safe_write(ai_dir / "final.md", final_md)
        print(f"run: failed during {failed_stage}. See {error_path}")
        raise

    final_status = test_summary.get("status", "ok") if final_passed else test_summary.get("status", "failed")
    recorder.complete_run(final_status, {"tests_passed": final_passed, "test_status": test_summary.get("status", "failed"), "validation_profile": validation_plan.profile})


# -----------------------------
# CLI
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Repo improvement pipeline (reasoner -> aider -> tests -> final report).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--config", default="", help="Ruta al config YAML/JSON.")
    sub = p.add_subparsers(dest="cmd")

    def add_shared(sp):
        sp.add_argument("--repo", default=".", help="Ruta del repo objetivo.")
        sp.add_argument("--goals", default="PROJECT_GOALS.md", help="Archivo de objetivos.")
        sp.add_argument("--ai-dir", default="AI", help="Directorio de artefactos AI.")
        sp.add_argument("--reasoner", default="", help="Modelo reasoner. Ej: ollama:deepseek-r1")
        sp.add_argument("--plan-mode", default="balanced", help="Estrategia de planning: fast, balanced o deep.")
        sp.add_argument("--plan-max-tree-lines", type=int, default=0, help="Override del máximo de líneas del árbol enviadas al reasoner.")
        sp.add_argument("--plan-max-files", type=int, default=0, help="Override del máximo de archivos incluidos en el contexto de planning.")
        sp.add_argument("--plan-max-file-chars", type=int, default=0, help="Override del máximo de caracteres por archivo en planning.")
        sp.add_argument("--plan-max-changes", type=int, default=0, help="Override del máximo de cambios que debe proponer el plan.")
        sp.add_argument("--plan-fallback-reasoner", default="", help="Modelo alternativo para planning si el principal entra en timeout.")
        sp.add_argument("--plan-fallback-timeout", type=int, default=0, help="Timeout del fallback de planning. Si es 0, usa reasoner-timeout.")
        sp.add_argument("--plan-retry-on-timeout", action="store_true", help="Si se activa, reintenta planning con modo fast y/o fallback al hacer timeout.")
        sp.add_argument("--apply-max-plan-changes", type=int, default=0, help="Máximo de cambios del plan a implementar. Si es 0, usa modo automático por plan-mode.")
        sp.add_argument("--apply-limit-to-plan-files", action="store_true", help="Limita Aider a los archivos explícitamente listados en el plan o en --allowed-file.")
        sp.add_argument("--apply-enforce-plan-scope", action="store_true", help="Falla si Aider modifica archivos fuera del scope seleccionado.")
        sp.add_argument("--apply-skip-unsupported-plan-changes", action="store_true", help="Omite cambios del plan que parecen requerir archivos o dependencias no presentes en el repo.")
        sp.add_argument("--validation-profile", default="auto", help="Perfil de validación: auto, minimal, python_local, python_repo o strict.")
        sp.add_argument("--semantic-validation", action="store_true", help="Activa validación semántica localizada sobre archivos Python cambiados.")
        sp.add_argument("--semantic-validation-mode", default="auto", help="Modo de validación semántica: auto, ast, ruff o pyflakes.")
        sp.add_argument("--semantic-validation-strict", action="store_true", help="Si se activa, los hallazgos semánticos fallan la corrida.")
        sp.add_argument("--semantic-validation-timeout", type=int, default=60, help="Timeout para herramientas opcionales de validación semántica.")
        sp.add_argument("--aider-model", default="", help="Modelo para Aider. Ej: ollama:qwen3-coder:14b")
        sp.add_argument("--reasoner-timeout", type=int, default=1800, help="Timeout en segundos para el reasoner del plan.")
        sp.add_argument("--report-timeout", type=int, default=0, help="Timeout en segundos para el reporte final. Si es 0, usa reasoner-timeout.")
        sp.add_argument("--aider-timeout", type=int, default=7200, help="Timeout en segundos para Aider.")
        sp.add_argument("--test-timeout", type=int, default=1800, help="Timeout en segundos para lint/tests.")
        sp.add_argument("--test-cmd", default="", help="Comando de tests.")
        sp.add_argument("--lint-cmd", default="", help="Comando de lint.")
        sp.add_argument("--lint-required", action="store_true", help="Si se activa, lint también bloquea el pipeline.")
        sp.add_argument("--allow-dirty-repo", action="store_true", help="Permite correr sobre un repo con cambios sin commit.")
        sp.add_argument("--max-iters", type=int, default=2, help="Máximo de iteraciones Aider->tests.")
        sp.add_argument("--zip", action="store_true", help="Crear snapshot zip.")
        sp.add_argument("--branch", default="", help="Nombre de la rama a crear.")
        sp.add_argument("--base", default="", help="Rama/ref base para apply/run.")
        sp.add_argument("--base-ref", default="", help="Base ref para report/diff.")
        sp.add_argument("--allowed-file", action="append", default=[], help="Archivo permitido para Aider (repetible).")
        sp.add_argument("--aider-extra-arg", action="append", default=[], help="Argumento extra para Aider (repetible).")
        sp.add_argument("--extra-context", action="append", default=[], help="Archivo adicional de contexto para el plan.")
        sp.add_argument("--max-tree-lines", type=int, default=600, help="Máximo de líneas del árbol del repo.")

    sp_snapshot = sub.add_parser("snapshot", help="Crear snapshot zip del repo.")
    add_shared(sp_snapshot)
    sp_snapshot.add_argument("--ref", default="HEAD", help="Git ref a archivar.")
    sp_snapshot.add_argument("--out", default="", help="Ruta de salida para el zip.")

    sp_plan = sub.add_parser("plan", help="Generar AI/plan.md con el reasoner.")
    add_shared(sp_plan)
    sp_plan.add_argument("--plan-out", default="", help="Ruta de salida del plan.")

    sp_apply = sub.add_parser("apply", help="Aplicar AI/plan.md con Aider.")
    add_shared(sp_apply)

    sp_test = sub.add_parser("test", help="Ejecutar lint/tests y generar test-report.")
    add_shared(sp_test)

    sp_report = sub.add_parser("report", help="Generar AI/final.md.")
    add_shared(sp_report)

    sp_run = sub.add_parser("run", help="Ejecutar pipeline completo.")
    add_shared(sp_run)

    return p


# -----------------------------
# Main
# -----------------------------

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cfg, cfg_path = load_config_file(getattr(args, "config", "") or None)

    if not getattr(args, "cmd", None):
        if cfg:
            args.cmd = "run"
        else:
            parser.print_help()
            raise SystemExit(1)

    if cfg:
        args = merge_config_into_args(args, cfg)
    args._config_path = cfg_path

    try:
        if args.cmd == "snapshot":
            cmd_snapshot(args)
        elif args.cmd == "plan":
            cmd_plan(args)
        elif args.cmd == "apply":
            cmd_apply(args)
        elif args.cmd == "test":
            cmd_test(args)
        elif args.cmd == "report":
            cmd_report(args)
        elif args.cmd == "run":
            cmd_run(args)
        else:
            raise SystemExit(f"Unknown command: {args.cmd}")
    except subprocess.TimeoutExpired:
        raise SystemExit("Timed out running a command (model/aider/tests).")
    except RuntimeError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
