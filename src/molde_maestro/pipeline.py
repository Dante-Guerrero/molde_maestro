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
import dataclasses
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Optional, Tuple, List, Any, Dict

from .git_ops import (
    collect_git_changed_files,
    collect_git_diff,
    ensure_git_repo,
    git_archive_zip,
    git_changed_files_between,
    git_checkout,
    git_create_branch,
    git_current_branch,
    git_head_commit,
    git_ref_exists,
    git_status_porcelain,
    infer_base_ref,
    resolve_base_branch,
)
from .models import (
    AIDER_FATAL_MARKERS,
    DEFAULT_AIDER_GITIGNORE_PATTERNS,
    ModelSpec,
    PlanAttemptSpec,
    PlanSettings,
    aider_output_has_fatal_error,
    build_reasoner_prompt,
    build_report_prompt,
    call_model,
    default_aider_instruction,
    ensure_repo_ready_for_aider,
    maybe_write_model_report,
    normalize_aider_model_name,
    parse_model_spec,
    run_aider,
    run_plan_generation,
    save_model_artifacts,
    stringify_command,
    validate_model_available,
)
from .reporting import (
    RunRecorder,
    clear_artifacts,
    init_run_context,
    project_reported_metadata,
    record_stage,
    write_stage_error,
)
from .utils import (
    ExecutionFailure,
    command_exists,
    ensure_ai_dir,
    format_duration,
    head_file,
    iso_now,
    looks_like_shell_command,
    normalize_model_markdown,
    now_stamp,
    read_text,
    repo_tree,
    require_nonempty,
    resolve_goals_path,
    run_cmd,
    safe_unlink,
    safe_write,
    sanitize_plan_commands,
    strip_ansi,
    to_text,
    truncate,
)
from .validation import (
    FunctionSignature,
    ValidationContext,
    ValidationPlan,
    build_function_signature,
    collect_local_function_signatures,
    detect_validation_commands,
    detect_validation_context,
    infer_validation_changed_files,
    module_path_from_import,
    python_module_available,
    python_module_name_for_path,
    repo_python_runner,
    resolve_imported_local_signatures,
    resolve_validation_plan,
    run_optional_semantic_tool,
    run_python_smoke_imports,
    run_semantic_validation,
    runner_executes_python,
    validation_plan_to_dict,
    write_test_report,
)


# -----------------------------
# Config loading (YAML/JSON)
# -----------------------------

def load_config_file(config_path: str | None) -> tuple[dict, Optional[Path]]:
    return cli_module.load_config_file(config_path)

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


def detect_validation_commands(repo: Path, args) -> List[str]:
    return importlib.import_module(".validation", __package__).detect_validation_commands(
        repo,
        args,
        resolve_validation_plan,
    )


def detect_validation_context(repo: Path) -> ValidationContext:
    return importlib.import_module(".validation", __package__).detect_validation_context(
        repo,
        runner_executes_python_fn=runner_executes_python,
        python_module_available_fn=python_module_available,
    )


def resolve_validation_plan(repo: Path, args) -> ValidationPlan:
    return importlib.import_module(".validation", __package__).resolve_validation_plan(
        repo,
        args,
        detect_validation_context_fn=detect_validation_context,
    )


def infer_validation_changed_files(repo: Path, args) -> Tuple[List[str], str]:
    return importlib.import_module(".validation", __package__).infer_validation_changed_files(
        repo,
        args,
        infer_base_ref_fn=infer_base_ref,
        collect_git_changed_files_fn=collect_git_changed_files,
        run_cmd_fn=run_cmd,
    )


def build_reasoner_prompt(
    repo: Path,
    goals_text: str,
    extra_context_files: List[str],
    settings: PlanSettings,
    validation_commands: List[str],
) -> str:
    return importlib.import_module(".models", __package__).build_reasoner_prompt(
        repo,
        goals_text,
        extra_context_files,
        settings,
        validation_commands,
        fast_plan_structure=FAST_PLAN_STRUCTURE,
        plan_template=PLAN_TEMPLATE,
        select_plan_context_files=select_plan_context_files,
    )


def maybe_write_model_report(
    ai_dir: Path,
    reasoner: str,
    prompt: str,
    repo: Path,
    timeout: int,
) -> Dict[str, Any]:
    return importlib.import_module(".models", __package__).maybe_write_model_report(
        ai_dir,
        reasoner,
        prompt,
        repo,
        timeout,
        call_model_fn=call_model,
    )


def run_plan_generation(
    repo: Path,
    ai_dir: Path,
    goals_text: str,
    args,
) -> Tuple[str, Dict[str, Any]]:
    return importlib.import_module(".models", __package__).run_plan_generation(
        repo,
        ai_dir,
        goals_text,
        args,
        build_plan_attempt_specs=build_plan_attempt_specs,
        resolve_plan_settings=resolve_plan_settings,
        detect_validation_commands=detect_validation_commands,
        plan_template=PLAN_TEMPLATE,
        fast_plan_structure=FAST_PLAN_STRUCTURE,
        select_plan_context_files=select_plan_context_files,
        call_model_fn=call_model,
    )


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
    return importlib.import_module(".validation", __package__).write_test_report(
        repo,
        ai_dir,
        lint_cmd,
        test_cmd,
        lint_required=lint_required,
        timeout=timeout,
        changed_files=changed_files,
        semantic_validation=semantic_validation,
        semantic_validation_mode=semantic_validation_mode,
        semantic_validation_strict=semantic_validation_strict,
        semantic_validation_timeout=semantic_validation_timeout,
        validation_plan=validation_plan,
        audit_python_dependency_declarations=audit_python_dependency_declarations,
        run_cmd_fn=run_cmd,
        detect_validation_context_fn=detect_validation_context,
        run_semantic_validation_fn=run_semantic_validation,
        run_python_smoke_imports_fn=run_python_smoke_imports,
    )


def merge_config_into_args(args: argparse.Namespace, cfg: dict) -> argparse.Namespace:
    return cli_module.merge_config_into_args(args, cfg)


# -----------------------------
# Model interaction
# -----------------------------

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


def dependency_name_from_requirement(requirement: str) -> str:
    requirement = requirement.strip()
    if not requirement:
        return ""
    requirement = requirement.split(";", 1)[0].strip()
    requirement = re.split(r"[<>=!~\[]", requirement, maxsplit=1)[0].strip()
    return requirement.lower()


def extract_dependency_names_from_pyproject(pyproject_path: Path) -> set[str]:
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()

    project = data.get("project")
    if not isinstance(project, dict):
        return set()

    deps: set[str] = set()
    dependency_groups: List[Any] = [project.get("dependencies", [])]
    optional_dependencies = project.get("optional-dependencies", {})
    if isinstance(optional_dependencies, dict):
        dependency_groups.extend(optional_dependencies.values())

    for group in dependency_groups:
        if not isinstance(group, list):
            continue
        for raw_dependency in group:
            if not isinstance(raw_dependency, str):
                continue
            normalized = dependency_name_from_requirement(raw_dependency)
            if normalized:
                deps.add(normalized)
    return deps


def load_repo_dependency_names(repo: Path) -> set[str]:
    deps: set[str] = set()
    requirements = repo / "requirements.txt"
    if requirements.exists():
        for raw_line in requirements.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            pkg = dependency_name_from_requirement(line)
            if pkg:
                deps.add(pkg)
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        deps.update(extract_dependency_names_from_pyproject(pyproject))
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


def effective_report_timeout(args) -> int:
    return args.report_timeout or args.reasoner_timeout


# -----------------------------
# Aider preparation / interaction
# -----------------------------


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
    validation_command: Optional[str] = None,
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
        command_match = re.search(r"Command:\n```bash\n(.+?)\n```", test_report_md, re.DOTALL)
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
        if command_match:
            validation_command = command_match.group(1).strip()
    validation_evidence_line = (
        f"- Current validation only proves the repo still compiles under `{validation_command}`."
        if validation_command
        else "- Current validation evidence is limited; inspect AI/test-report.md for the exact executed command."
    )
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
            validation_evidence_line,
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
        validate_model_available(parse_model_spec(args.reasoner), run_cmd)
        if args.cmd in {"plan", "run"} and args.plan_fallback_reasoner.strip():
            validate_model_available(parse_model_spec(args.plan_fallback_reasoner), run_cmd)

    if args.cmd in {"apply", "run"} and args.aider_model.strip():
        aider_model = parse_model_spec(args.aider_model)
        if aider_model.kind != "ollama":
            raise SystemExit("aider_model debe usar formato ollama:<modelo>.")
        validate_model_available(aider_model, run_cmd)

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
# Commands / CLI compatibility
# -----------------------------
from . import cli as cli_module


def cmd_snapshot(args) -> None:
    from .commands.snapshot import cmd_snapshot as impl

    impl(args)


def cmd_plan(args) -> None:
    from .commands.plan import cmd_plan as impl

    impl(args)


def cmd_apply(args) -> None:
    from .commands.apply import cmd_apply as impl

    impl(args)


def cmd_test(args) -> None:
    from .commands.test import cmd_test as impl

    impl(args)


def cmd_report(args) -> None:
    from .commands.report import cmd_report as impl

    impl(args)


def cmd_run(args) -> None:
    from .commands.run import cmd_run as impl

    impl(args)


# -----------------------------
# CLI
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    return cli_module.build_parser()


# -----------------------------
# Main
# -----------------------------

def main() -> None:
    cli_module.main()


if __name__ == "__main__":
    main()
