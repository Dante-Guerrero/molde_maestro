from __future__ import annotations

import dataclasses
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional

from .reporting import clear_artifacts, write_stage_error
from .utils import ExecutionFailure, command_exists, head_file, normalize_model_markdown, read_text, repo_tree, safe_write, sanitize_plan_commands, to_text, truncate


@dataclasses.dataclass
class ModelSpec:
    kind: str
    name: str


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


def stringify_command(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def call_model(model: ModelSpec, prompt: str, cwd: Optional[Path] = None, timeout: int = 1800) -> str:
    if model.kind == "ollama":
        args = ["ollama", "run", model.name]
    else:
        args = shlex.split(model.name)
    try:
        proc = subprocess.run(args, input=prompt, cwd=str(cwd) if cwd else None, text=True, capture_output=True, timeout=timeout, env=os.environ.copy())
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


def validate_model_available(model: ModelSpec, run_cmd) -> None:
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


def save_model_artifacts(ai_dir: Path, prefix: str, prompt: str, raw: Optional[str] = None) -> None:
    safe_write(ai_dir / f"{prefix}-prompt.txt", prompt)
    if raw is not None:
        safe_write(ai_dir / f"{prefix}-raw.txt", raw)


def ensure_repo_ready_for_aider(repo: Path) -> None:
    gitignore = repo / ".gitignore"
    existing = read_text(gitignore, default="")
    missing = [p for p in DEFAULT_AIDER_GITIGNORE_PATTERNS if p not in existing]
    if missing:
        parts = [existing.rstrip()] if existing.strip() else []
        parts.append("# Aider / local artifacts")
        parts.extend(missing)
        safe_write(gitignore, "\n".join(parts).rstrip() + "\n")


def run_aider(
    repo: Path,
    aider_model: str,
    message: str,
    *,
    files: Optional[list[str]] = None,
    log_path: Optional[Path] = None,
    extra_args: Optional[list[str]] = None,
    timeout: int = 7200,
) -> tuple[int, str, str]:
    base_cmd = [
        "aider",
        "--model", normalize_aider_model_name(aider_model),
        "--auto-commits",
        "--yes-always",
        "--no-show-model-warnings",
        "--no-check-model-accepts-settings",
    ]
    if files:
        for path in files:
            base_cmd += ["--file", path]
    base_cmd += ["--message", message]
    if extra_args:
        base_cmd += extra_args

    try:
        proc = subprocess.run(base_cmd, cwd=str(repo), text=True, capture_output=True, timeout=timeout, env=os.environ.copy())
    except subprocess.TimeoutExpired as exc:
        stdout = to_text(exc.stdout)
        stderr = to_text(exc.stderr)
        if log_path:
            existing = read_text(log_path, default="")
            block = (f"{existing}\n\n" if existing.strip() else "") + (
                f"# Aider run\n\n## CMD\n```bash\n{stringify_command(base_cmd)}\n```\n\n"
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
        existing = read_text(log_path, default="")
        block = (f"{existing}\n\n" if existing.strip() else "") + (
            f"# Aider run\n\n## CMD\n```bash\n{stringify_command(base_cmd)}\n```\n\n"
            f"## RETURN CODE\n{proc.returncode}\n\n"
            f"## STDOUT\n```text\n{proc.stdout}\n```\n\n"
            f"## STDERR\n```text\n{proc.stderr}\n```\n"
        )
        safe_write(log_path, block)
    return proc.returncode, proc.stdout, proc.stderr


def aider_output_has_fatal_error(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}"
    return any(marker in combined for marker in AIDER_FATAL_MARKERS)


def build_reasoner_prompt(
    repo: Path,
    goals_text: str,
    extra_context_files: list[str],
    settings: PlanSettings,
    validation_commands: list[str],
    *,
    fast_plan_structure: str,
    plan_template: str,
    select_plan_context_files,
) -> str:
    tree = repo_tree(repo, max_lines=settings.max_tree_lines)
    selected = select_plan_context_files(repo, extra_context_files, settings.max_files)
    key_blobs: list[str] = []
    for path in selected:
        key_blobs.append(f"\n\n### FILE: {path}\n```text\n{head_file(repo, path, max_chars=settings.max_file_chars)}\n```")
    validation_block = "\n".join(f"- {cmd}" for cmd in validation_commands) or "- [no clear validation command detected]"
    if settings.mode == "fast":
        return f"""\
You are a senior engineer preparing a short implementation plan for a repository.
Output ONLY Markdown for plan.md. No preamble. No code fences around the whole answer.

Use EXACTLY this structure:
{fast_plan_structure}

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
{plan_template}

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
    allowed_files: Optional[list[str]] = None,
    validation_commands: Optional[list[str]] = None,
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


def build_report_prompt(goals_text: str, plan_text: str, test_report_md: str, git_diff: str, run_metadata: str = "") -> str:
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


def maybe_write_model_report(ai_dir: Path, reasoner: str, prompt: str, repo: Path, timeout: int, *, call_model_fn=call_model) -> dict[str, Any]:
    details: dict[str, Any] = {
        "prompt_path": str(ai_dir / "report-prompt.txt"),
        "model_report_path": None,
        "raw_path": None,
        "model_summary_status": "skipped",
    }
    if not reasoner.strip():
        details["model_summary_note"] = "No reasoner configured for optional report summary."
        return details
    save_model_artifacts(ai_dir, "report", prompt)
    details["raw_path"] = str(ai_dir / "report-raw.txt")
    details["model_report_path"] = str(ai_dir / "report-model.md")
    try:
        raw = call_model_fn(parse_model_spec(reasoner), prompt, cwd=repo, timeout=timeout)
    except BaseException as exc:
        error_path = write_stage_error(ai_dir, "report-model", exc, {"report_timeout": timeout, "reasoner": reasoner})
        details["model_summary_status"] = "failed"
        details["model_summary_note"] = str(exc)
        details["model_summary_error_path"] = str(error_path)
        return details
    save_model_artifacts(ai_dir, "report", prompt, raw)
    safe_write(ai_dir / "report-model.md", normalize_model_markdown(raw, "# Final Report"))
    details["model_summary_status"] = "ok"
    return details


def run_plan_generation(
    repo: Path,
    ai_dir: Path,
    goals_text: str,
    args,
    *,
    build_plan_attempt_specs,
    resolve_plan_settings,
    detect_validation_commands,
    plan_template: str,
    fast_plan_structure: str,
    select_plan_context_files,
    call_model_fn=call_model,
) -> tuple[str, dict[str, Any]]:
    attempts = build_plan_attempt_specs(args)
    validation_commands = detect_validation_commands(repo, args)
    clear_artifacts(ai_dir, ["plan.md", "plan-prompt.txt", "plan-raw.txt", "plan-error.md", "plan-attempts.json"])
    attempt_records: list[dict[str, Any]] = []
    last_exc: Optional[BaseException] = None
    for attempt in attempts:
        settings = resolve_plan_settings(args, mode_override=attempt.mode)
        prompt = build_reasoner_prompt(repo, goals_text, args.extra_context, settings, validation_commands, fast_plan_structure=fast_plan_structure, plan_template=plan_template, select_plan_context_files=select_plan_context_files)
        attempt_prefix = f"plan-attempt-{attempt.index}"
        save_model_artifacts(ai_dir, attempt_prefix, prompt)
        safe_write(ai_dir / "plan-prompt.txt", prompt)
        print(f"plan: attempt {attempt.index}/{len(attempts)} label={attempt.label} mode={attempt.mode} reasoner={attempt.reasoner}")
        record: dict[str, Any] = {
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
            raw = call_model_fn(parse_model_spec(attempt.reasoner), prompt, cwd=repo, timeout=attempt.timeout)
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
