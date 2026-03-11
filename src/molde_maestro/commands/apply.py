from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from .. import pipeline as core
from ..command_config import ApplyCommandConfig
from .. import terminal_ui


def cmd_apply(args) -> None:
    config = ApplyCommandConfig.from_args(args)
    repo = Path(config.repo).expanduser().resolve()
    core.preflight(config._raw_args, repo)
    repo, ai_dir, recorder = core.init_run_context(config._raw_args)

    core.require_nonempty(config.aider_model, "aider_model", "Config: aider_model: 'ollama:qwen3-coder:14b'")

    plan_path = ai_dir / "plan.md"
    plan_text = core.read_text(plan_path, default="")
    if not plan_text.strip():
        raise SystemExit(f"apply: falta o está vacío {plan_path}. Ejecuta `plan` primero.")
    git_ctx = core.initialize_git_run_context(repo, config._raw_args, plan_text)
    core.attach_git_run_context(recorder, git_ctx)
    dependency_report: dict[str, Any] = {"dependency_changes_detected": False}
    final_changed_files: list[str] = []

    try:
        with core.record_stage(recorder, "apply") as details:
            core.clear_artifacts(ai_dir, ["aider-log.txt", "aider-message.txt", "apply-error.md", "apply-scope.json", "apply-selected-plan.md"])
            git_ctx, prep, pre_apply_head, active_branch = core.prepare_apply_git_session(repo, recorder, git_ctx, config._raw_args)

            core.ensure_repo_ready_for_aider(repo)

            apply_scope = core.resolve_apply_scope(repo, plan_text, config._raw_args)
            validation_commands = core.detect_validation_commands(repo, config._raw_args)
            aider_msg = core.default_aider_instruction(
                plan_text,
                selected_plan_md=apply_scope.selected_plan_md,
                allowed_files=apply_scope.selected_files,
                validation_commands=validation_commands,
                plan_mode=config.plan_mode,
            )
            core.safe_write(ai_dir / "aider-message.txt", aider_msg)
            core.safe_write(ai_dir / "apply-selected-plan.md", apply_scope.selected_plan_md or "[No selected plan subset]\n")
            core.safe_write(
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

            rc, stdout, err = core.run_aider(
                repo=repo,
                aider_model=config.aider_model,
                message=aider_msg,
                files=apply_scope.selected_files or None,
                log_path=log_path,
                extra_args=config.aider_extra_args,
                timeout=config.aider_timeout,
            )
            details["active_branch"] = active_branch
            details["log_path"] = str(log_path)
            details["aider_timeout"] = config.aider_timeout
            details["apply_scope_path"] = str(ai_dir / "apply-scope.json")
            details["selected_plan_path"] = str(ai_dir / "apply-selected-plan.md")
            details["apply_scope_source"] = apply_scope.source
            details["selected_files"] = apply_scope.selected_files
            details["selected_change_titles"] = [change.title for change in apply_scope.selected_changes]
            details["skipped_change_titles"] = apply_scope.skipped_change_titles
            details["cleaned_tracked_noise_files"] = prep["cleaned_tracked_noise_files"]
            details["preexisting_commit"] = prep["preexisting_commit"]
            details["original_branch"] = git_ctx.original_branch
            details["work_branch"] = git_ctx.work_branch
            details["base_ref"] = git_ctx.base_ref
            details["head_before_run"] = git_ctx.head_before_run
            details["stdout_excerpt"] = core.truncate(stdout, 2000)
            if apply_scope.selected_files:
                terminal_ui.print_kv_summary(
                    "Scope de apply:",
                    {
                        "source": apply_scope.source,
                        "archivos seleccionados": len(apply_scope.selected_files),
                        "cambios seleccionados": len(apply_scope.selected_changes),
                    },
                )
            if rc != 0 or core.aider_output_has_fatal_error(stdout, err):
                raise core.ExecutionFailure(
                    f"Aider failed ({rc}).",
                    command=f"aider --model {core.normalize_aider_model_name(config.aider_model)}",
                    returncode=rc,
                    stdout=stdout,
                    stderr=err,
                    status="failed",
                )
            aider_result = core.collect_aider_result(repo, pre_apply_head)
            changed_files = aider_result["changed_files"]
            details["aider_result_mode"] = aider_result["mode"]
            details["aider_commit_created"] = aider_result["commit_created"]
            details["head_before_apply"] = aider_result["head_before"]
            details["head_after_apply"] = aider_result["head_after"]
            if not changed_files or set(changed_files) <= {".gitignore"}:
                raise core.ExecutionFailure(
                    "Aider completed without producing a meaningful change.",
                    command=f"aider --model {core.normalize_aider_model_name(config.aider_model)}",
                    stdout=stdout,
                    stderr=err,
                    status="failed",
                )
            if config.apply_enforce_plan_scope and apply_scope.selected_files:
                scope_violations = [path for path in changed_files if path not in apply_scope.selected_files and path != ".gitignore"]
                if scope_violations:
                    raise core.ExecutionFailure(
                        "Aider changed files outside the selected apply scope.",
                        command=f"aider --model {core.normalize_aider_model_name(config.aider_model)}",
                        stdout=stdout,
                        stderr=err,
                        status="failed",
                    )
            dep_audit = core.audit_python_dependency_declarations(repo, changed_files)
            details["dependency_audit"] = dep_audit
            if not dep_audit["ok"]:
                dep_resolution = core.resolve_missing_python_dependencies(repo, dep_audit, changed_files)
                details["dependency_resolution"] = dep_resolution
                dependency_report = dep_resolution
                core.persist_dependency_report(recorder, dep_resolution)
                if dep_resolution.get("decision") == "aborted":
                    raise core.ExecutionFailure(
                        "La corrida fue abortada por dependencias nuevas pendientes de aprobacion.",
                        command=f"aider --model {core.normalize_aider_model_name(config.aider_model)}",
                        stdout=stdout,
                        stderr=json.dumps(dep_resolution, ensure_ascii=False),
                        status="failed",
                    )
                if dep_resolution.get("dependency_changes_applied"):
                    aider_result = core.reconcile_dependency_resolution(repo, pre_apply_head, aider_result, dep_resolution)
                    changed_files = aider_result["changed_files"]
                    details["aider_result_mode"] = aider_result["mode"]
                    details["aider_commit_created"] = aider_result["commit_created"]
                    details["head_after_apply"] = aider_result["head_after"]
                    details["dependency_resolution_commit"] = aider_result.get("dependency_commit")
            details["changed_files"] = changed_files
            final_changed_files = changed_files
            terminal_ui.print_status("info", f"Aider finalizo con codigo {rc}")
            print(core.explain_completed_changes(details["selected_change_titles"], changed_files))
            commit_sha = None
            if not aider_result["commit_created"]:
                commit_sha = core.confirm_and_commit_changes(
                    repo,
                    "El plan fue aplicado sobre la rama activa.",
                    core.generated_commit_message("feat", details["selected_change_titles"]),
                    changed_files,
                )
            details["result_commit"] = commit_sha
            details["dirty_after_apply"] = core.git_has_working_tree_changes(repo)
            git_ctx = dataclasses.replace(git_ctx, head_after_run=core.git_head_commit(repo))
            core.attach_git_run_context(recorder, git_ctx)
    except BaseException as exc:
        error_path = core.write_stage_error(ai_dir, "apply", exc, {"aider_timeout": config.aider_timeout})
        recorder.fail_run(
            str(exc),
            "timeout" if isinstance(exc, core.ExecutionFailure) and exc.status == "timeout" else "failed",
            {"stage": "apply", "error_path": str(error_path)},
        )
        terminal_ui.print_human_error_summary("apply", str(exc), error_path, hint="Revisa el artefacto de error para el detalle tecnico.")
        raise
    final_status = core.classify_final_status(None, dependency_report)
    core.finalize_apply_like_command(
        recorder,
        final_status=final_status,
        git_ctx=git_ctx,
        ai_dir=ai_dir,
        changed_files=final_changed_files,
        dependency_report=dependency_report,
        summary_details={
            "plan_path": str(plan_path),
        },
    )
