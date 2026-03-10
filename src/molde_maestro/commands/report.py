from __future__ import annotations

import json

from .. import pipeline as core
from ..command_config import ReportCommandConfig


def cmd_report(args) -> None:
    config = ReportCommandConfig.from_args(args)
    repo, ai_dir, recorder = core.init_run_context(config._raw_args)
    core.preflight(config._raw_args, repo)

    goals_path = core.resolve_goals_path(repo, config.goals)
    goals_text = core.read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n")

    plan_text = core.read_text(ai_dir / "plan.md", default="")
    test_report_md = core.read_text(ai_dir / "test-report.md", default="[Missing test report]")
    run_metadata = core.read_text(ai_dir / "run-metadata.json", default="[Missing run metadata]")
    if not plan_text.strip():
        raise SystemExit("report: falta AI/plan.md")

    try:
        with core.record_stage(recorder, "report") as details:
            core.clear_artifacts(ai_dir, ["final.md", "report-prompt.txt", "report-raw.txt", "report-model.md", "report-error.md"])
            base_ref = config.base_ref.strip() or core.infer_base_ref(repo)
            git_diff = core.collect_git_diff(repo, base_ref)
            changed_files = core.collect_git_changed_files(repo, base_ref)

            prompt = core.build_report_prompt(goals_text, plan_text, test_report_md, git_diff, run_metadata)
            metadata_payload = json.loads(run_metadata) if run_metadata.strip().startswith("{") else recorder.metadata
            final_md = core.build_grounded_final_report(
                core.project_reported_metadata(metadata_payload, metadata_payload.get("status")),
                plan_text,
                test_report_md,
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
                    prompt,
                    repo,
                    core.effective_report_timeout(config._raw_args),
                )
            )
            print(f"report: wrote {ai_dir / 'final.md'} (base_ref={base_ref})")
    except BaseException as exc:
        error_path = core.write_stage_error(ai_dir, "report", exc, {"report_timeout": core.effective_report_timeout(config._raw_args)})
        recorder.fail_run(
            str(exc),
            "timeout" if isinstance(exc, core.ExecutionFailure) and exc.status == "timeout" else "failed",
            {"stage": "report", "error_path": str(error_path)},
        )
        print(f"report: failed. See {error_path}")
        raise
    recorder.complete_run("ok", {"final_report": str(ai_dir / "final.md")})
