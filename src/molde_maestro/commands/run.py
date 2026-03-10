from __future__ import annotations

import json
from typing import Any

from .. import pipeline as core
from ..command_config import RunCommandConfig


def cmd_run(args) -> None:
    config = RunCommandConfig.from_args(args)
    repo, ai_dir, recorder = core.init_run_context(config._raw_args)
    core.preflight(config._raw_args, repo)

    core.require_nonempty(config.reasoner, "reasoner", "Config: reasoner: 'ollama:deepseek-r1'")
    core.require_nonempty(config.aider_model, "aider_model", "Config: aider_model: 'ollama:qwen3-coder:14b'")

    if config.zip_enabled:
        with core.record_stage(recorder, "snapshot") as details:
            out_zip = ai_dir / "snapshots" / f"{core.now_stamp()}.zip"
            core.git_archive_zip(repo, out_zip)
            details["artifact"] = str(out_zip)
            print(f"run: snapshot zip -> {out_zip}")
    else:
        recorder.mark_stage_skipped("snapshot", "zip_disabled")

    goals_path = core.resolve_goals_path(repo, config.goals)
    goals_text = core.read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n")
    try:
        with core.record_stage(recorder, "plan") as details:
            plan_md, plan_meta = core.run_plan_generation(repo, ai_dir, goals_text, config._raw_args)
            details["artifact"] = str(ai_dir / "plan.md")
            details["prompt_path"] = str(ai_dir / "plan-prompt.txt")
            details["raw_path"] = str(ai_dir / "plan-raw.txt")
            details.update(plan_meta)
            print(f"run: plan -> {ai_dir / 'plan.md'}")

        validation_plan = core.resolve_validation_plan(repo, config._raw_args)
        lint_cmd = validation_plan.lint_command
        test_cmd = validation_plan.test_command
        last_feedback = ""
        final_passed = False
        test_summary: dict[str, Any] = {"status": "failed"}

        with core.record_stage(recorder, "apply") as details:
            core.clear_artifacts(ai_dir, ["aider-log.txt", "aider-message.txt", "apply-error.md", "apply-scope.json", "apply-selected-plan.md", "test-report.md", "test-report.json", "test-error.md", "final.md", "report-prompt.txt", "report-raw.txt", "report-model.md", "report-error.md"])
            base = core.resolve_base_branch(repo, config.base)
            branch = config.branch.strip() or f"ai/{core.now_stamp()}-improvements"
            core.git_checkout(repo, base)
            core.git_create_branch(repo, branch)
            print(f"run: branch -> {branch} (from {base})")
            pre_apply_head = core.git_head_commit(repo)

            core.ensure_repo_ready_for_aider(repo)

            aider_log = ai_dir / "aider-log.txt"
            apply_scope = core.resolve_apply_scope(repo, plan_md, config._raw_args)
            validation_commands = core.detect_validation_commands(repo, config._raw_args)
            aider_msg = core.default_aider_instruction(
                plan_md,
                selected_plan_md=apply_scope.selected_plan_md,
                allowed_files=apply_scope.selected_files,
                validation_commands=validation_commands,
                plan_mode=config.plan_mode,
            )
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
            details["base"] = base
            details["branch"] = branch
            details["log_path"] = str(aider_log)
            details["aider_timeout"] = config.aider_timeout
            details["apply_scope_path"] = str(ai_dir / "apply-scope.json")
            details["selected_plan_path"] = str(ai_dir / "apply-selected-plan.md")
            details["apply_scope_source"] = apply_scope.source
            details["selected_files"] = apply_scope.selected_files
            details["selected_change_titles"] = [change.title for change in apply_scope.selected_changes]
            details["skipped_change_titles"] = apply_scope.skipped_change_titles

            for i in range(1, config.max_iters + 1):
                msg = aider_msg if i == 1 else (
                    aider_msg + "\n\nAlso fix the following test/lint failures:\n" + core.truncate(last_feedback, 12000)
                )
                core.safe_write(ai_dir / "aider-message.txt", msg)
                rc, stdout, err = core.run_aider(
                    repo=repo,
                    aider_model=config.aider_model,
                    message=msg,
                    files=apply_scope.selected_files or None,
                    log_path=aider_log,
                    extra_args=config.aider_extra_args,
                    timeout=config.aider_timeout,
                )
                if rc != 0 or core.aider_output_has_fatal_error(stdout, err):
                    raise core.ExecutionFailure(
                        f"Aider failed on iteration {i} ({rc}).",
                        command=f"aider --model {core.normalize_aider_model_name(config.aider_model)}",
                        returncode=rc,
                        stdout=stdout,
                        stderr=err,
                        status="failed",
                    )
                post_apply_head = core.git_head_commit(repo)
                changed_files = core.git_changed_files_between(repo, pre_apply_head, post_apply_head)
                if post_apply_head == pre_apply_head or not changed_files or set(changed_files) <= {".gitignore"}:
                    raise core.ExecutionFailure(
                        "Aider completed without producing a meaningful committed change.",
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
                    raise core.ExecutionFailure(
                        "Aider introduced Python imports whose dependencies are not declared in the repo.",
                        command=f"aider --model {core.normalize_aider_model_name(config.aider_model)}",
                        stdout=stdout,
                        stderr=json.dumps(dep_audit, ensure_ascii=False),
                        status="failed",
                    )
                details["last_iteration"] = i
                details["stdout_excerpt"] = core.truncate(stdout, 2000)
                details["changed_files"] = changed_files

                with core.record_stage(recorder, "test") as test_details:
                    core.clear_artifacts(ai_dir, ["test-report.md", "test-report.json", "test-error.md"])
                    passed, test_report_md, test_summary = core.write_test_report(
                        repo,
                        ai_dir,
                        lint_cmd,
                        test_cmd,
                        lint_required=config.lint_required,
                        timeout=config.test_timeout,
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
                    test_details["test_timeout"] = config.test_timeout
                    test_details["validation_profile"] = test_summary.get("validation_profile")
                    test_details["summary"] = test_summary
                    print(f"run: iter {i} tests passed={passed}")
                if passed:
                    final_passed = True
                    break

                last_feedback = test_report_md

        if not (ai_dir / "test-report.md").exists():
            core.safe_write(ai_dir / "test-report.md", "[No test report produced]\n")
            recorder.mark_stage_skipped("test", "no_test_report_produced")

        if not final_passed:
            print("run: se agotaron las iteraciones sin conseguir un estado verde completo.")

        with core.record_stage(recorder, "report") as details:
            core.clear_artifacts(ai_dir, ["final.md", "report-prompt.txt", "report-raw.txt", "report-model.md", "report-error.md"])
            base_ref = core.infer_base_ref(repo)
            git_diff = core.collect_git_diff(repo, base_ref)
            changed_files = core.collect_git_changed_files(repo, base_ref)
            report_prompt = core.build_report_prompt(
                goals_text,
                plan_md,
                core.read_text(ai_dir / "test-report.md"),
                git_diff,
                core.read_text(ai_dir / "run-metadata.json"),
            )
            projected_status = "ok" if final_passed else test_summary.get("status", "failed")
            final_md = core.build_grounded_final_report(
                core.project_reported_metadata(recorder.metadata, projected_status),
                plan_md,
                core.read_text(ai_dir / "test-report.md"),
                changed_files,
                git_diff,
            )
            core.safe_write(ai_dir / "final.md", final_md)
            details["artifact"] = str(ai_dir / "final.md")
            details["base_ref"] = base_ref
            details["report_timeout"] = core.effective_report_timeout(config._raw_args)
            details.update(
                core.maybe_write_model_report(
                    ai_dir,
                    config.reasoner,
                    report_prompt,
                    repo,
                    core.effective_report_timeout(config._raw_args),
                )
            )
            print(f"run: final -> {ai_dir / 'final.md'} (base_ref={base_ref})")
    except BaseException as exc:
        failed_stage = next((stage["name"] for stage in reversed(recorder.metadata["stages"]) if stage["status"] in {"running", "failed", "timeout"}), "run")
        error_path = core.write_stage_error(ai_dir, failed_stage, exc)
        status = "timeout" if isinstance(exc, core.ExecutionFailure) and exc.status == "timeout" else "failed"
        recorder.fail_run(str(exc), status, {"stage": failed_stage, "error_path": str(error_path)})
        final_md = core.build_fallback_final_report(
            recorder.metadata,
            core.read_text(ai_dir / "plan.md"),
            core.read_text(ai_dir / "test-report.md"),
        )
        core.safe_write(ai_dir / "final.md", final_md)
        print(f"run: failed during {failed_stage}. See {error_path}")
        raise

    final_status = test_summary.get("status", "ok") if final_passed else test_summary.get("status", "failed")
    recorder.complete_run(final_status, {"tests_passed": final_passed, "test_status": test_summary.get("status", "failed"), "validation_profile": validation_plan.profile})
