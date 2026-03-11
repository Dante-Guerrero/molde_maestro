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
    cleanup_tracked_noise_artifacts,
    cleanup_untracked_noise_artifacts,
    collect_git_changed_files,
    collect_git_diff,
    collect_working_tree_changed_files,
    collect_working_tree_diff,
    ensure_git_repo,
    git_commit_paths,
    git_archive_zip,
    git_commit_all,
    git_current_branch,
    git_current_branch_or_none,
    git_create_or_reset_branch,
    git_has_working_tree_changes,
    git_head_commit,
    git_init_with_initial_commit,
    git_switch_or_checkout,
    is_git_repo,
    git_ref_exists,
    git_status_porcelain,
    infer_base_ref,
    resolve_base_branch,
    tracked_noise_files,
    untracked_noise_artifacts,
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
from . import terminal_ui
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


@dataclasses.dataclass(frozen=True)
class GoalsInput:
    text: str
    source: str
    path: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class GitRunContext:
    original_branch: Optional[str]
    work_branch: Optional[str]
    base_ref: str
    base_ref_source: str
    head_before_run: str
    head_after_run: Optional[str] = None
    dirty_repo_allowed: bool = False
    unsafe_shell: bool = False


def slugify_branch_hint(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower())
    normalized = normalized.strip("-.")
    return normalized[:32] or "run"


def generated_ai_branch_name(args, goals_text: str = "") -> str:
    hint_source = ""
    if goals_text.strip():
        for line in goals_text.splitlines():
            cleaned = line.strip(" #-*\t")
            if cleaned:
                hint_source = cleaned
                break
    if not hint_source:
        hint_source = getattr(args, "cmd", "run") or "run"
    return f"ai/{now_stamp()}-{slugify_branch_hint(hint_source)}"


def detect_original_branch(repo: Path) -> Optional[str]:
    branch = git_current_branch_or_none(repo)
    if branch:
        return branch
    return None


def resolve_effective_base_ref(repo: Path, args, original_branch: Optional[str]) -> tuple[str, str]:
    requested = str(getattr(args, "base", "") or getattr(args, "base_ref", "") or "").strip()
    if requested:
        if not git_ref_exists(repo, requested):
            raise SystemExit(f"La rama/ref base no existe: {requested}")
        return requested, "cli"
    if original_branch and git_ref_exists(repo, original_branch):
        return original_branch, "original_branch"
    return infer_base_ref(repo), "auto"


def resolve_work_branch(repo: Path, args, original_branch: Optional[str], goals_text: str = "") -> tuple[str, str]:
    requested = str(getattr(args, "branch", "") or "").strip()
    if requested:
        return requested, "cli"
    current = git_current_branch_or_none(repo)
    if current and current.startswith("ai/") and sys.stdin.isatty():
        terminal_ui.print_section("Rama AI detectada")
        terminal_ui.print_status("info", f"Estas en {current}.")
        if terminal_ui.confirm_action("¿Reutilizar esta rama de trabajo?"):
            return current, "reuse_current_ai"
    return generated_ai_branch_name(args, goals_text), "generated"


def checkout_work_branch(repo: Path, branch_name: str, start_ref: str) -> None:
    if git_ref_exists(repo, branch_name):
        git_switch_or_checkout(repo, branch_name)
        return
    git_create_or_reset_branch(repo, branch_name, start_ref)


def initialize_git_run_context(repo: Path, args, goals_text: str = "") -> GitRunContext:
    original_branch = detect_original_branch(repo)
    head_before_run = git_head_commit(repo)
    base_ref, base_ref_source = resolve_effective_base_ref(repo, args, original_branch)
    work_branch, _branch_source = resolve_work_branch(repo, args, original_branch, goals_text)
    return GitRunContext(
        original_branch=original_branch,
        work_branch=work_branch,
        base_ref=base_ref,
        base_ref_source=base_ref_source,
        head_before_run=head_before_run,
        dirty_repo_allowed=bool(getattr(args, "allow_dirty_repo", False)),
        unsafe_shell=bool(getattr(args, "unsafe_shell", False)),
    )


def activate_git_run_context(repo: Path, context: GitRunContext) -> GitRunContext:
    if context.work_branch:
        checkout_work_branch(repo, context.work_branch, context.head_before_run)
    return context


def attach_git_run_context(recorder: RunRecorder, context: GitRunContext) -> None:
    recorder.metadata["original_branch"] = context.original_branch
    recorder.metadata["work_branch"] = context.work_branch
    recorder.metadata["base_ref"] = context.base_ref
    recorder.metadata["base_ref_source"] = context.base_ref_source
    recorder.metadata["head_before_run"] = context.head_before_run
    recorder.metadata["head_after_run"] = context.head_after_run
    recorder.metadata["dirty_repo_allowed"] = context.dirty_repo_allowed
    recorder.metadata["unsafe_shell"] = context.unsafe_shell
    recorder.metadata.setdefault("dependency_changes_detected", False)
    recorder.metadata.setdefault("dependency_changes_applied", False)
    recorder.metadata.setdefault("dependency_approval_required", False)
    recorder.metadata.setdefault("suggested_dependencies", [])
    recorder.metadata.setdefault("approved_dependencies", [])
    recorder.metadata.setdefault("rejected_dependencies", [])
    recorder.save()


def prepare_apply_git_session(repo: Path, recorder: RunRecorder, git_ctx: GitRunContext, args) -> tuple[GitRunContext, dict[str, Any], str, str]:
    active_branch = git_current_branch(repo)
    prep = ensure_repo_ready_for_apply_session(repo, args)
    git_ctx = activate_git_run_context(repo, git_ctx)
    attach_git_run_context(recorder, git_ctx)
    pre_apply_head = git_ctx.head_before_run
    return git_ctx, prep, pre_apply_head, active_branch


def persist_dependency_report(recorder: RunRecorder, dep_report: dict[str, Any]) -> None:
    recorder.metadata["dependency_report"] = dep_report
    recorder.metadata["dependency_changes_detected"] = dep_report.get("dependency_changes_detected", False)
    recorder.metadata["dependency_changes_applied"] = dep_report.get("dependency_changes_applied", False)
    recorder.metadata["dependency_approval_required"] = dep_report.get("dependency_approval_required", False)
    recorder.metadata["suggested_dependencies"] = dep_report.get("suggested_dependencies", [])
    recorder.metadata["approved_dependencies"] = dep_report.get("approved_dependencies", [])
    recorder.metadata["rejected_dependencies"] = dep_report.get("rejected_dependencies", [])
    recorder.save()


def finalize_apply_like_command(
    recorder: RunRecorder,
    *,
    final_status: str,
    git_ctx: GitRunContext,
    ai_dir: Path,
    changed_files: list[str],
    dependency_report: Optional[dict[str, Any]] = None,
    summary_details: Optional[dict[str, Any]] = None,
) -> None:
    details = {
        "work_branch": git_ctx.work_branch,
        "original_branch": git_ctx.original_branch,
        "base_ref": git_ctx.base_ref,
    }
    if summary_details:
        details.update(summary_details)
    recorder.complete_run(final_status, details)
    print(
        render_run_completion_summary(
            final_status=final_status,
            original_branch=git_ctx.original_branch,
            work_branch=git_ctx.work_branch,
            base_ref=git_ctx.base_ref,
            changed_files=changed_files,
            ai_dir=ai_dir,
            dependency_report=dependency_report,
        )
    )


def tracked_hygiene_violations(repo: Path) -> list[str]:
    return tracked_noise_files(repo)


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


def has_inline_goals(args) -> bool:
    return bool(str(getattr(args, "goals_text", "") or "").strip())


def resolve_goals_input(repo: Path, ai_dir: Path, args) -> GoalsInput:
    inline_goals = str(getattr(args, "goals_text", "") or "").strip()
    if inline_goals:
        session_goals_path = ai_dir / "session-goals.md"
        safe_write(session_goals_path, inline_goals.rstrip() + "\n")
        source = "interactive_input" if getattr(args, "_interactive_goals_text", False) else "goals_text"
        return GoalsInput(text=inline_goals, source=source, path=str(session_goals_path))

    goals_path = resolve_goals_path(repo, getattr(args, "goals", "PROJECT_GOALS.md"))
    return GoalsInput(
        text=read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n"),
        source="file",
        path=str(goals_path),
    )


def confirm_noise_cleanup(repo: Path, label: str, paths: list[str]) -> bool:
    if not paths:
        return False
    if not sys.stdin.isatty():
        raise SystemExit(
            "Se detectó limpieza necesaria en el repo objetivo, pero la sesión no es interactiva.\n"
            "Vuelve a correr en una terminal interactiva para responder y/n."
        )
    sample = ", ".join(paths[:5])
    extra = "" if len(paths) <= 5 else f" ... (+{len(paths) - 5} más)"
    terminal_ui.print_section("Limpieza sugerida")
    terminal_ui.print_status("warn", f"Se detectaron artefactos generados ({label}) en {repo}.")
    terminal_ui.print_status("info", f"Muestra: {sample}{extra}")
    terminal_ui.print_status("info", "Se eliminaran solo artefactos generados detectados.")
    return terminal_ui.confirm_action("¿Continuar con la limpieza?")


def format_changed_files_summary(paths: list[str], limit: int = 10) -> str:
    if not paths:
        return "(sin archivos)"
    shown = paths[:limit]
    body = "\n".join(f"- {path}" for path in shown)
    if len(paths) > limit:
        body += f"\n- ... y {len(paths) - limit} más"
    return body


def is_ai_artifact_path(path: str, ai_dir_name: str = "AI") -> bool:
    normalized = path.strip().lstrip("./")
    ai_dir_name = (ai_dir_name or "AI").strip("/").lstrip("./") or "AI"
    return normalized == ai_dir_name or normalized.startswith(f"{ai_dir_name}/")


def filter_user_visible_paths(paths: list[str], ai_dir_name: str = "AI") -> list[str]:
    return [path for path in paths if not is_ai_artifact_path(path, ai_dir_name)]


def explain_completed_changes(selected_titles: list[str], changed_files: list[str], test_status: Optional[str] = None) -> str:
    lines = ["Resumen de cambios:"]
    if selected_titles:
        lines.append("Cambios aplicados:")
        lines.extend(f"- {title}" for title in selected_titles)
    if changed_files:
        lines.append("Archivos modificados:")
        lines.extend(f"- {path}" for path in changed_files[:10])
        if len(changed_files) > 10:
            lines.append(f"- ... y {len(changed_files) - 10} más")
    if test_status:
        lines.append(f"Estado de validación: {test_status}")
    return "\n".join(lines)


def generated_commit_message(prefix: str, selected_titles: list[str]) -> str:
    if selected_titles:
        first = re.sub(r"\s+", " ", selected_titles[0].strip().rstrip("."))
        candidate = f"{prefix}: {first}"
        return candidate[:72]
    return f"{prefix}: apply planned changes"


def confirm_and_commit_changes(repo: Path, prompt: str, commit_message: str, changed_files: list[str]) -> Optional[str]:
    if not changed_files or not sys.stdin.isatty():
        return None
    terminal_ui.print_section("Commit sugerido")
    terminal_ui.print_status("info", prompt)
    terminal_ui.print_list("Archivos incluidos:", changed_files)
    if not terminal_ui.confirm_action("¿Continuar con el commit?"):
        return None
    commit_sha = git_commit_paths(repo, commit_message, changed_files)
    terminal_ui.print_status("ok", f"Commit creado: {commit_sha}")
    return commit_sha


def ensure_repo_ready_for_apply_session(repo: Path, args, *, tracked_noise: Optional[list[str]] = None) -> dict[str, Any]:
    cleaned_noise_files: list[str] = []
    tracked_noise = tracked_noise if tracked_noise is not None else tracked_noise_files(repo)
    if tracked_noise and confirm_noise_cleanup(repo, "tracked", tracked_noise):
        cleaned_noise_files = cleanup_tracked_noise_artifacts(repo)

    dirty_files = filter_user_visible_paths(collect_working_tree_changed_files(repo), getattr(args, "ai_dir", "AI"))
    result: dict[str, Any] = {
        "cleaned_tracked_noise_files": cleaned_noise_files,
        "preexisting_changed_files": dirty_files,
        "preexisting_commit": None,
    }
    if not dirty_files:
        return result
    if getattr(args, "allow_dirty_repo", False):
        return result
    if not sys.stdin.isatty():
        raise SystemExit(
            "El repo objetivo tiene cambios sin commit y la sesión no es interactiva.\n"
            "Haz commit/stash primero o activa --allow-dirty-repo."
        )

    terminal_ui.print_section("Repo con cambios previos")
    terminal_ui.print_status("warn", "Se detectaron cambios antes de aplicar el plan.")
    terminal_ui.print_list("Archivos detectados:", dirty_files)
    commit_sha = confirm_and_commit_changes(
        repo,
        "Molde Maestro recomienda guardar el estado actual antes de modificar la rama activa.",
        "chore: checkpoint before molde_maestro",
        dirty_files,
    )
    if commit_sha is None:
        raise SystemExit("Operación cancelada para no mezclar cambios previos con los cambios del plan.")
    result["preexisting_commit"] = commit_sha
    result["preexisting_changed_files"] = []
    return result


def collect_report_changed_files(repo: Path, base_ref: str) -> list[str]:
    return filter_user_visible_paths(collect_working_tree_changed_files(repo)) or filter_user_visible_paths(collect_git_changed_files(repo, base_ref))


def collect_report_diff(repo: Path, base_ref: str) -> str:
    return collect_working_tree_diff(repo) or collect_git_diff(repo, base_ref)


def extract_apply_stage_details(metadata: dict[str, Any]) -> dict[str, Any]:
    for stage in metadata.get("stages", []):
        if stage.get("name") == "apply":
            return stage.get("details", {})
    return {}


def collect_report_context(repo: Path, base_ref: str, metadata: Optional[dict[str, Any]] = None, ai_dir_name: str = "AI") -> tuple[list[str], str]:
    metadata = metadata or {}
    apply_details = extract_apply_stage_details(metadata)
    apply_changed_files = filter_user_visible_paths(apply_details.get("changed_files") or [], ai_dir_name)
    if apply_changed_files:
        apply_base = str(apply_details.get("head_before_apply") or "").strip()
        if apply_base:
            return apply_changed_files, collect_git_diff(repo, apply_base)
        return apply_changed_files, collect_report_diff(repo, base_ref)
    return collect_report_changed_files(repo, base_ref), collect_report_diff(repo, base_ref)


def collect_aider_result(repo: Path, previous_head: str) -> dict[str, Any]:
    current_head = git_head_commit(repo)
    working_tree_files = filter_user_visible_paths(collect_working_tree_changed_files(repo))
    if working_tree_files:
        return {
            "mode": "working_tree",
            "changed_files": working_tree_files,
            "commit_created": False,
            "head_before": previous_head,
            "head_after": current_head,
        }
    if current_head != previous_head:
        committed_files = filter_user_visible_paths(collect_git_changed_files(repo, previous_head))
        return {
            "mode": "commit",
            "changed_files": committed_files,
            "commit_created": True,
            "head_before": previous_head,
            "head_after": current_head,
        }
    return {
        "mode": "none",
        "changed_files": [],
        "commit_created": False,
        "head_before": previous_head,
        "head_after": current_head,
    }


def ensure_git_repo_ready(repo: Path) -> None:
    if is_git_repo(repo):
        return
    if not sys.stdin.isatty():
        raise SystemExit(f"No es un repo git válido: {repo}")
    terminal_ui.print_section("Repositorio sin git")
    terminal_ui.print_status("warn", f"El repositorio objetivo no tiene git inicializado: {repo}")
    terminal_ui.print_status("info", "Se creara un repo git con commit inicial.")
    if not terminal_ui.confirm_action("¿Continuar con la inicializacion?"):
        raise SystemExit("Operación cancelada: el repo objetivo no tiene git.")
    commit_sha = git_init_with_initial_commit(repo)
    terminal_ui.print_status("ok", f"Repositorio git inicializado: {commit_sha}")


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


def local_python_import_roots(repo: Path) -> set[str]:
    roots: set[str] = set()
    candidate_bases = [repo]
    src_dir = repo / "src"
    if src_dir.exists():
        candidate_bases.append(src_dir)
    for base in candidate_bases:
        if not base.exists() or not base.is_dir():
            continue
        for child in base.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_file() and child.suffix == ".py" and child.stem != "__init__":
                roots.add(child.stem)
                continue
            if not child.is_dir():
                continue
            if base == src_dir or (child / "__init__.py").exists():
                roots.add(child.name)
    return roots


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
    local_roots = {name.lower() for name in local_python_import_roots(repo)}
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
            if lowered in stdlib or lowered in declared or lowered in local_roots:
                continue
            missing.append(root)
        if missing:
            issues.append({"file": rel_path, "missing_dependencies": missing})
    return {"ok": not issues, "issues": issues}


def collect_missing_python_dependencies(dep_audit: Dict[str, Any]) -> list[str]:
    deps: list[str] = []
    for issue in dep_audit.get("issues", []):
        for raw_name in issue.get("missing_dependencies", []):
            name = dependency_name_from_requirement(str(raw_name))
            if name:
                deps.append(name)
    return list(dict.fromkeys(sorted(deps)))


def detect_dependency_file_strategy(repo: Path) -> dict[str, Any]:
    has_pyproject = (repo / "pyproject.toml").exists()
    has_requirements = (repo / "requirements.txt").exists()
    if has_pyproject and has_requirements:
        return {
            "system": "both",
            "target_file": "pyproject.toml",
            "target_path": str(repo / "pyproject.toml"),
            "will_create": False,
        }
    if has_pyproject:
        return {
            "system": "pyproject.toml",
            "target_file": "pyproject.toml",
            "target_path": str(repo / "pyproject.toml"),
            "will_create": False,
        }
    if has_requirements:
        return {
            "system": "requirements.txt",
            "target_file": "requirements.txt",
            "target_path": str(repo / "requirements.txt"),
            "will_create": False,
        }
    return {
        "system": "none",
        "target_file": "requirements.txt",
        "target_path": str(repo / "requirements.txt"),
        "will_create": True,
    }


def update_requirements_file(requirements_path: Path, dependencies: list[str]) -> list[str]:
    existing_text = requirements_path.read_text(encoding="utf-8") if requirements_path.exists() else ""
    existing_lines = existing_text.splitlines()
    existing_names = {
        dependency_name_from_requirement(line)
        for line in existing_lines
        if line.strip() and not line.strip().startswith("#")
    }
    additions = [dep for dep in dependencies if dep not in existing_names]
    if not additions:
        return []
    lines = list(existing_lines)
    lines.extend(additions)
    safe_write(requirements_path, "\n".join(lines).rstrip() + "\n")
    return additions


def _format_pyproject_dependency_block(dependencies: list[str]) -> str:
    if not dependencies:
        return "dependencies = []\n"
    lines = ["dependencies = ["]
    lines.extend(f'    "{dep}",' for dep in dependencies)
    lines.append("]")
    return "\n".join(lines) + "\n"


def _find_toml_array_end(text: str, start_index: int) -> int:
    if start_index < 0 or start_index >= len(text) or text[start_index] != "[":
        raise ExecutionFailure("Invalid TOML array start while updating pyproject.toml.")
    depth = 0
    quote_char = ""
    escape = False
    for index in range(start_index, len(text)):
        char = text[index]
        if quote_char:
            if quote_char == '"':
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == quote_char:
                    quote_char = ""
            elif char == quote_char:
                quote_char = ""
            continue
        if char in {'"', "'"}:
            quote_char = char
            continue
        if char == "[":
            depth += 1
            continue
        if char == "]":
            depth -= 1
            if depth == 0:
                return index
    raise ExecutionFailure("Could not find the end of project.dependencies in pyproject.toml.")


def _append_pyproject_dependency_items(items: str, additions: list[str]) -> str:
    rendered_additions = [f'"{dep}"' for dep in additions]
    if "\n" in items:
        trailing_ws_len = len(items) - len(items.rstrip())
        trailing_ws = items[-trailing_ws_len:] if trailing_ws_len else ""
        content = items[:-trailing_ws_len] if trailing_ws_len else items
        indent_match = re.search(r"(?m)^(\s*)['\"]", content)
        indent = indent_match.group(1) if indent_match else "    "
        if content and not content.endswith("\n"):
            content += "\n"
        content += "".join(f"{indent}{item},\n" for item in rendered_additions)
        return content + trailing_ws
    stripped = items.strip()
    if not stripped:
        return ", ".join(rendered_additions)
    return f"{stripped}, {', '.join(rendered_additions)}"


def update_pyproject_dependencies(pyproject_path: Path, dependencies: list[str]) -> list[str]:
    text = pyproject_path.read_text(encoding="utf-8")
    parsed_dependencies = extract_dependency_names_from_pyproject(pyproject_path)
    additions = [dep for dep in dependencies if dep not in parsed_dependencies]
    if not additions:
        return []
    if "[project]" not in text:
        suffix = "" if not text.strip() else "\n\n"
        text = text.rstrip() + suffix + "[project]\n" + _format_pyproject_dependency_block(additions)
        safe_write(pyproject_path, text.rstrip() + "\n")
        return additions

    section_match = re.search(r"(?ms)^\[project\]\n(?P<body>.*?)(?=^\[|\Z)", text)
    if not section_match:
        raise ExecutionFailure("Could not locate [project] section in pyproject.toml.")

    body = section_match.group("body")
    dependency_match = re.search(r"(?m)^dependencies\s*=\s*\[", body)
    if dependency_match:
        array_start = body.find("[", dependency_match.start())
        if array_start < 0:
            raise ExecutionFailure("Could not locate project.dependencies array in pyproject.toml.")
        array_end = _find_toml_array_end(body, array_start)
        items = body[array_start + 1:array_end]
        updated_items = _append_pyproject_dependency_items(items, additions)
        replacement = f"dependencies = [{updated_items}]"
        new_body = body[:dependency_match.start()] + replacement + body[array_end + 1:]
    else:
        dependency_block = _format_pyproject_dependency_block(additions)
        new_body = dependency_block + body

    new_section = "[project]\n" + new_body
    new_text = text[:section_match.start()] + new_section + text[section_match.end():]
    safe_write(pyproject_path, new_text.rstrip() + "\n")
    return additions


def add_missing_python_dependencies(repo: Path, dependencies: list[str], strategy: dict[str, Any]) -> dict[str, Any]:
    target_file = strategy["target_file"]
    target_path = repo / target_file
    if target_file == "pyproject.toml":
        added = update_pyproject_dependencies(target_path, dependencies)
    else:
        added = update_requirements_file(target_path, dependencies)
    return {
        "updated_file": target_file,
        "updated_path": str(target_path),
        "added_dependencies": added,
        "created_file": bool(strategy.get("will_create") and target_path.exists()),
    }


def import_name_to_package_name(import_name: str) -> str:
    lowered = dependency_name_from_requirement(import_name)
    reverse_aliases = {
        alias: package_name
        for package_name, aliases in DECLARED_DEP_ALIASES.items()
        for alias in aliases
    }
    return reverse_aliases.get(lowered, lowered)


def detect_dependency_changes(repo: Path, changed_files: List[str], dep_audit: Dict[str, Any]) -> dict[str, Any]:
    strategy = detect_dependency_file_strategy(repo)
    runner, _runner_source = repo_python_runner(repo)
    declared = declared_import_names(repo)
    stdlib = {name.lower() for name in getattr(sys, "stdlib_module_names", set())}
    local_roots = {name.lower() for name in local_python_import_roots(repo)}
    suggestions: list[dict[str, Any]] = []
    suggestion_index: dict[tuple[str, str], dict[str, Any]] = {}

    def record_suggestion(package_name: str, kind: str, file_path: str, import_name: str, reason: str, *, confidence: str = "high") -> None:
        key = (package_name, kind)
        entry = suggestion_index.get(key)
        if entry is None:
            entry = {
                "package": package_name,
                "kind": kind,
                "reason": reason,
                "files": [],
                "imports": [],
                "confidence": confidence,
                "alternative": "Considera reimplementar la solucion sin libreria nueva si el cambio es pequeno." if kind == "new_suggested_dependency" else "",
            }
            suggestion_index[key] = entry
            suggestions.append(entry)
        if file_path not in entry["files"]:
            entry["files"].append(file_path)
        if import_name and import_name not in entry["imports"]:
            entry["imports"].append(import_name)

    for issue in dep_audit.get("issues", []):
        file_path = str(issue.get("file") or "")
        for missing_name in issue.get("missing_dependencies", []):
            package_name = import_name_to_package_name(str(missing_name))
            record_suggestion(
                package_name,
                "new_suggested_dependency",
                file_path,
                str(missing_name),
                f"Import nuevo no declarado detectado en {file_path}.",
            )

    for rel_path in changed_files:
        if not rel_path.endswith(".py"):
            continue
        source = read_text(repo / rel_path)
        for import_root in sorted(extract_import_roots(source)):
            lowered = import_root.lower()
            if lowered in stdlib or lowered in local_roots or lowered not in declared:
                continue
            if runner and not python_module_available(runner, import_root, repo):
                record_suggestion(
                    import_name_to_package_name(import_root),
                    "declared_but_not_installed",
                    rel_path,
                    import_root,
                    f"El import {import_root} aparece declarado pero no se pudo importar con el runner detectado.",
                    confidence="medium",
                )

    requires_approval = any(item["kind"] == "new_suggested_dependency" for item in suggestions)
    suggested_dependencies = [item["package"] for item in suggestions if item["kind"] == "new_suggested_dependency"]
    return {
        "ok": not suggestions,
        "dependency_changes_detected": bool(suggestions),
        "dependency_changes_applied": False,
        "dependency_approval_required": requires_approval,
        "suggested_dependencies": suggested_dependencies,
        "approved_dependencies": [],
        "rejected_dependencies": [],
        "pending_dependencies": suggested_dependencies[:] if requires_approval else [],
        "uncertain_dependencies": [item["package"] for item in suggestions if item.get("confidence") != "high"],
        "strategy": strategy,
        "suggestions": suggestions,
        "updated_file": None,
        "updated_path": None,
        "created_file": False,
        "added_dependencies": [],
        "decision": "none" if not suggestions else "pending",
        "message": "" if not suggestions else "Se detectaron dependencias nuevas o no instaladas que requieren decision explicita.",
    }


def render_dependency_notice(dep_report: dict[str, Any]) -> str:
    suggestions = dep_report.get("suggestions") or []
    if not dep_report.get("dependency_changes_detected") and not suggestions:
        return "No se detectaron cambios de dependencias."
    lines = ["Estado de dependencias detectadas:"]
    for suggestion in suggestions:
        files_text = ", ".join(suggestion.get("files") or []) or "sin archivos"
        imports_text = ", ".join(suggestion.get("imports") or []) or "sin imports"
        lines.append(
            f"- {suggestion['package']}: kind={suggestion['kind']} files={files_text} imports={imports_text} reason={suggestion['reason']}"
        )
    decision = dep_report.get("decision", "pending")
    lines.append(f"- decision: {decision}")
    if dep_report.get("added_dependencies"):
        lines.append(f"- dependencias agregadas: {', '.join(dep_report['added_dependencies'])}")
    if dep_report.get("rejected_dependencies"):
        lines.append(f"- dependencias rechazadas: {', '.join(dep_report['rejected_dependencies'])}")
    if dep_report.get("pending_dependencies"):
        lines.append(f"- dependencias pendientes: {', '.join(dep_report['pending_dependencies'])}")
    return "\n".join(lines)


def prompt_dependency_decision(dep_report: dict[str, Any]) -> str:
    terminal_ui.print_section("Dependencias nuevas detectadas")
    terminal_ui.print_status("warn", "La solucion aplicada introdujo dependencias nuevas o no instaladas.")
    for suggestion in dep_report.get("suggestions", []):
        terminal_ui.print_status(
            "info",
            (
                f"{suggestion['package']} ({suggestion['kind']}) en {', '.join(suggestion.get('files') or ['sin archivos'])}. "
                f"Motivo: {suggestion['reason']}"
            ),
        )
        if suggestion.get("alternative"):
            terminal_ui.print_status("info", f"Alternativa: {suggestion['alternative']}")
    terminal_ui.print_status("info", "1 = aprobar incorporacion, 2 = rechazar y continuar, 3 = abortar corrida.")
    while True:
        choice = input("> ").strip()
        if choice == "1":
            return "approved"
        if choice == "2":
            return "rejected"
        if choice == "3":
            return "aborted"
        terminal_ui.print_status("warn", "Entrada invalida. Usa 1, 2 o 3.", stream=sys.stderr)


def resolve_missing_python_dependencies(repo: Path, dep_audit: Dict[str, Any], changed_files: Optional[List[str]] = None) -> dict[str, Any]:
    dep_report = detect_dependency_changes(repo, changed_files or [], dep_audit)
    if not dep_report["dependency_changes_detected"]:
        return dep_report
    if not dep_report["dependency_approval_required"]:
        dep_report["ok"] = True
        dep_report["decision"] = "observed"
        dep_report["message"] = "Se detectaron dependencias declaradas pero no instaladas o hallazgos no bloqueantes."
        return dep_report
    if not sys.stdin.isatty():
        dep_report["ok"] = True
        dep_report["decision"] = "pending"
        dep_report["message"] = (
            "Se detectaron dependencias nuevas no declaradas. En modo no interactivo no se editaran manifiestos; la decision queda pendiente."
        )
        return dep_report

    decision = prompt_dependency_decision(dep_report)
    dep_report["decision"] = decision
    if decision == "aborted":
        dep_report["ok"] = False
        dep_report["message"] = "El usuario aborto la corrida ante dependencias nuevas."
        return dep_report
    if decision == "rejected":
        dep_report["ok"] = True
        dep_report["rejected_dependencies"] = dep_report["suggested_dependencies"][:]
        dep_report["pending_dependencies"] = []
        dep_report["message"] = "El usuario rechazo editar archivos de dependencias."
        return dep_report

    strategy = dep_report["strategy"]
    update = add_missing_python_dependencies(repo, dep_report["suggested_dependencies"], strategy)
    dep_report.update(update)
    dep_report["dependency_changes_applied"] = bool(update.get("added_dependencies"))
    dep_report["approved_dependencies"] = list(update.get("added_dependencies") or [])
    dep_report["pending_dependencies"] = []
    dep_report["ok"] = True
    dep_report["message"] = "Dependencias nuevas aprobadas y agregadas al manifiesto."
    return dep_report


def reconcile_dependency_resolution(
    repo: Path,
    previous_head: str,
    aider_result: dict[str, Any],
    dep_resolution: dict[str, Any],
    commit_message: str = "chore: declare added Python dependencies",
) -> dict[str, Any]:
    dep_file = str(dep_resolution.get("updated_file") or "").strip()
    dependency_commit = None
    if dep_file and dep_resolution.get("added_dependencies") and aider_result.get("commit_created"):
        working_tree_files = set(collect_working_tree_changed_files(repo))
        if dep_file in working_tree_files:
            dependency_commit = git_commit_paths(repo, commit_message, [dep_file])
    refreshed = collect_aider_result(repo, previous_head)
    changed_files = list(refreshed.get("changed_files", []))
    if dep_file and dep_file not in changed_files:
        if (repo / dep_file).exists():
            changed_files.append(dep_file)
    refreshed["changed_files"] = changed_files
    refreshed["dependency_commit"] = dependency_commit
    refreshed["dependency_commit_created"] = dependency_commit is not None
    return refreshed


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


def ensure_safe_command_policy(args, validation_plan: Optional[ValidationPlan] = None) -> None:
    unsafe_shell = bool(getattr(args, "unsafe_shell", False))
    if str(getattr(args, "reasoner", "") or "").strip().startswith("cmd:") and not unsafe_shell:
        raise SystemExit("reasoner=cmd:... requiere --unsafe-shell.")
    if str(getattr(args, "plan_fallback_reasoner", "") or "").strip().startswith("cmd:") and not unsafe_shell:
        raise SystemExit("plan_fallback_reasoner=cmd:... requiere --unsafe-shell.")
    plan = validation_plan
    if plan is None and getattr(args, "cmd", "") in {"test", "run"}:
        plan = resolve_validation_plan(Path(getattr(args, "repo", ".")).expanduser().resolve(), args)
    if plan is None:
        return
    for label, cmd in (("test_cmd", plan.test_command), ("lint_cmd", plan.lint_command or "")):
        if cmd and looks_like_shell_command(cmd) and not unsafe_shell:
            raise SystemExit(f"{label} contiene metacaracteres de shell y requiere --unsafe-shell.")


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


def summarize_dependency_report(dep_report: Optional[dict[str, Any]]) -> list[str]:
    if not dep_report or not dep_report.get("dependency_changes_detected"):
        return ["- Dependency changes: none detected."]
    lines = [
        f"- Dependency decision: **{dep_report.get('decision', 'pending')}**.",
        f"- Suggested dependencies: {', '.join(dep_report.get('suggested_dependencies') or ['none'])}.",
        f"- Approved dependencies: {', '.join(dep_report.get('approved_dependencies') or ['none'])}.",
        f"- Rejected dependencies: {', '.join(dep_report.get('rejected_dependencies') or ['none'])}.",
    ]
    if dep_report.get("updated_file"):
        lines.append(f"- Updated dependency file: `{dep_report['updated_file']}`.")
    return lines


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
    dependency_report = apply_details.get("dependency_resolution") or metadata.get("dependency_report")
    original_branch = metadata.get("original_branch")
    work_branch = metadata.get("work_branch")
    base_ref = metadata.get("base_ref")
    selected_lines = [f"- {title}" for title in selected_titles] or ["- No selected change titles recorded."]
    test_summary_line = "- No test report was produced."
    semantic_summary_line = "- No semantic validation evidence."
    validation_profile_line = "- Validation profile not recorded."
    validation_evidence_line = "- Validation evidence level not recorded."
    checks_line = "- Validation checks not recorded."
    promotion_line = "- Promotion decision not recorded."
    runner_line = "- Runner not recorded."
    semantic_alert = False
    dependency_warning = bool(dependency_report and dependency_report.get("decision") in {"pending", "rejected", "aborted"})
    if test_report_md.strip():
        command_match = re.search(r"Command:\n```bash\n(.+?)\n```", test_report_md, re.DOTALL)
        match = re.search(r"- stage_status: \*\*(.+?)\*\*", test_report_md)
        semantic_match = re.search(r"- semantic_validation_status: \*\*(.+?)\*\*", test_report_md)
        profile_match = re.search(r"- validation_profile: \*\*(.+?)\*\*", test_report_md)
        promotion_match = re.search(r"- promotion_decision: \*\*(.+?)\*\*", test_report_md)
        runner_match = re.search(r"- runner: \*\*(.+?)\*\*", test_report_md)
        evidence_match = re.search(r"- validation_evidence_level: \*\*(.+?)\*\*", test_report_md)
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
        if evidence_match:
            validation_evidence_line = f"- Validation evidence level: **{evidence_match.group(1)}**."
        checks = re.findall(r"- L(\d+) `([^`]+)`: \*\*(.+?)\*\* blocking=(True|False)", test_report_md)
        if checks:
            checks_line = "- Validation checks: " + "; ".join(f"L{level} {name}={status}" for level, name, status, _ in checks[:6]) + "."
        if command_match:
            validation_command = command_match.group(1).strip()
    validation_scope_line = (
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
            f"- Original branch: **{original_branch or 'unknown'}**.",
            f"- Work branch: **{work_branch or 'unknown'}**.",
            f"- Base ref: **{base_ref or 'unknown'}**.",
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
            validation_evidence_line,
            promotion_line,
            runner_line,
            checks_line,
            validation_scope_line,
            *summarize_dependency_report(dependency_report),
            "",
            "## Risks and Regressions",
            "- This report is grounded on metadata and git diff to avoid hallucinated success claims.",
            "- A green compile check is weaker than behavioral tests; functional regressions may still exist.",
            "- Semantic validation reported suspicious Python issues." if semantic_alert else "- No semantic validation alerts were recorded.",
            "- Hay dependencias nuevas pendientes o rechazadas; la solucion puede requerir intervencion manual." if dependency_warning else "- No hay cambios de dependencias pendientes.",
            "- Review the actual diff before treating the change as complete.",
            "",
            "## Next Steps",
            "- Inspect the committed diff and decide whether the applied change is functionally sufficient.",
            "- Strengthen `test_cmd` when the target repo has installable dependencies or sample fixtures.",
            "- Resolver manualmente las dependencias pendientes o reemplazar la solucion por una alternativa sin librerias nuevas." if dependency_warning else "- No dependency follow-up was recorded.",
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


def classify_final_status(test_summary: Optional[dict[str, Any]], dep_report: Optional[dict[str, Any]] = None) -> str:
    base_status = str((test_summary or {}).get("status", "ok") or "ok")
    if base_status in {"failed", "timeout"}:
        return base_status
    evidence_level = str((test_summary or {}).get("validation_evidence_level", "") or "")
    dep_decision = str((dep_report or {}).get("decision", "") or "")
    if dep_decision in {"pending", "rejected", "aborted"}:
        return "warning"
    if base_status == "warning":
        return "warning"
    if evidence_level and evidence_level != "functional_tests":
        return "warning"
    return "ok"


def render_run_completion_summary(
    *,
    final_status: str,
    original_branch: Optional[str],
    work_branch: Optional[str],
    base_ref: Optional[str],
    changed_files: list[str],
    ai_dir: Path,
    dependency_report: Optional[dict[str, Any]] = None,
) -> str:
    lines = [f"Corrida completada con {final_status}.", ""]
    lines.append(f"Rama original: {original_branch or 'desconocida'}")
    lines.append(f"Rama de trabajo: {work_branch or 'desconocida'}")
    lines.append(f"Base de comparación: {base_ref or 'desconocida'}")
    lines.append("")
    lines.append("Archivos modificados:")
    if changed_files:
        lines.extend(f"- {path}" for path in changed_files[:10])
        if len(changed_files) > 10:
            lines.append(f"- ... y {len(changed_files) - 10} más")
    else:
        lines.append("- Sin cambios detectados.")
    lines.append("")
    lines.append("Artefactos generados:")
    for name in ("plan.md", "test-report.md", "final.md"):
        lines.append(f"- {ai_dir / name}")
    lines.append("")
    lines.append(render_dependency_notice(dependency_report or {"dependency_changes_detected": False}))
    if original_branch:
        lines.append("")
        lines.append("Para volver a tu rama original:")
        lines.append(f"  git switch {original_branch}")
    if base_ref:
        lines.append("")
        lines.append("Para revisar ahora:")
        lines.append("  git status")
        lines.append(f"  git diff {base_ref}...HEAD")
        lines.append(f"  cat {ai_dir / 'final.md'}")
    if original_branch and work_branch:
        lines.append("")
        lines.append("Para hacer merge después de revisar:")
        lines.append(f"  git switch {original_branch}")
        lines.append(f"  git merge {work_branch}")
    return "\n".join(lines)

# -----------------------------
# Validation / preflight
# -----------------------------

def preflight(args, repo: Path) -> None:
    ensure_git_repo_ready(repo)

    if not command_exists("git"):
        raise SystemExit("No se encontró 'git' en PATH.")

    ensure_safe_command_policy(args)

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
        if not has_inline_goals(args) and not goals_path.exists():
            raise SystemExit(f"No existe el archivo de goals: {goals_path}")

    if args.cmd in {"test", "run"}:
        validation_plan = resolve_validation_plan(repo, args)
        ensure_safe_command_policy(args, validation_plan)
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
        untracked_noise = untracked_noise_artifacts(repo)
        if untracked_noise and confirm_noise_cleanup(repo, "untracked", untracked_noise):
            cleanup_untracked_noise_artifacts(repo)
        dirty = git_status_porcelain(repo)
        if dirty and not sys.stdin.isatty():
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
