from __future__ import annotations

from .. import pipeline as core
from ..command_config import TestCommandConfig
from .. import terminal_ui


def cmd_test(args) -> None:
    config = TestCommandConfig.from_args(args)
    repo, ai_dir, recorder = core.init_run_context(config._raw_args)
    core.preflight(config._raw_args, repo)

    validation_plan = core.resolve_validation_plan(repo, config._raw_args)
    changed_files, changed_base_ref = core.infer_validation_changed_files(repo, config._raw_args)
    try:
        with core.record_stage(recorder, "test") as details:
            core.clear_artifacts(ai_dir, ["test-report.md", "test-report.json", "test-error.md"])
            passed, _, summary = core.write_test_report(
                repo,
                ai_dir,
                validation_plan.lint_command,
                validation_plan.test_command,
                lint_required=config.lint_required,
                timeout=config.test_timeout,
                changed_files=changed_files,
                semantic_validation=validation_plan.semantic_validation,
                semantic_validation_mode=validation_plan.semantic_validation_mode,
                semantic_validation_strict=validation_plan.semantic_validation_strict,
                semantic_validation_timeout=validation_plan.semantic_validation_timeout,
                validation_plan=validation_plan,
            )
            details["__status"] = summary["status"]
            details["artifact"] = str(ai_dir / "test-report.md")
            details["test_timeout"] = config.test_timeout
            details["changed_files_base_ref"] = changed_base_ref
            details["changed_files"] = changed_files
            details["validation_profile"] = summary["validation_profile"]
            details["summary"] = summary
            kind = "ok" if passed else "warn"
            terminal_ui.print_status(kind, f"Validacion completada. Reporte: {ai_dir / 'test-report.md'}")
    except BaseException as exc:
        error_path = core.write_stage_error(ai_dir, "test", exc, {"test_timeout": config.test_timeout})
        recorder.fail_run(
            str(exc),
            "timeout" if isinstance(exc, core.ExecutionFailure) and exc.status == "timeout" else "failed",
            {"stage": "test", "error_path": str(error_path)},
        )
        terminal_ui.print_human_error_summary("test", str(exc), error_path, hint="Revisa el reporte y el artefacto de error.")
        raise
    recorder.complete_run(summary["status"], {"passed": passed, "test_report": str(ai_dir / "test-report.md"), "validation_profile": validation_plan.profile})
    if not passed:
        raise SystemExit(124 if summary["status"] == "timeout" else 2)
