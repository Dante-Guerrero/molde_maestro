from __future__ import annotations

from pathlib import Path

from .. import pipeline as core
from ..command_config import PlanCommandConfig


def cmd_plan(args) -> None:
    config = PlanCommandConfig.from_args(args)
    repo, ai_dir, recorder = core.init_run_context(config._raw_args)
    core.preflight(config._raw_args, repo)

    core.require_nonempty(config.reasoner, "reasoner", "Config: reasoner: 'ollama:deepseek-r1'")
    goals_input = core.resolve_goals_input(repo, ai_dir, config._raw_args)

    plan_out = Path(config.plan_out).expanduser() if config.plan_out else ai_dir / "plan.md"
    if not plan_out.is_absolute():
        plan_out = repo / plan_out

    try:
        with core.record_stage(recorder, "plan") as details:
            plan_md, plan_meta = core.run_plan_generation(repo, ai_dir, goals_input.text, config._raw_args)
            if plan_out != ai_dir / "plan.md":
                core.safe_write(plan_out, plan_md)
            details["artifact"] = str(plan_out)
            details["prompt_path"] = str(ai_dir / "plan-prompt.txt")
            details["raw_path"] = str(ai_dir / "plan-raw.txt")
            details["sanitized_path"] = str(plan_out)
            details["goals_source"] = goals_input.source
            details["goals_path"] = goals_input.path
            details.update(plan_meta)
            print(f"plan: wrote {plan_out}")
    except BaseException as exc:
        error_path = core.write_stage_error(
            ai_dir,
            "plan",
            exc,
            {
                "reasoner_timeout": config.reasoner_timeout,
                "plan_mode": config.plan_mode,
                "plan_fallback_reasoner": config.plan_fallback_reasoner,
                "plan_fallback_timeout": core.effective_plan_fallback_timeout(config._raw_args),
                "plan_attempts_path": str(ai_dir / "plan-attempts.json"),
            },
        )
        recorder.fail_run(
            str(exc),
            "timeout" if isinstance(exc, core.ExecutionFailure) and exc.status == "timeout" else "failed",
            {"stage": "plan", "error_path": str(error_path)},
        )
        print(f"plan: failed. See {error_path}")
        raise
    recorder.complete_run("ok", {"plan_path": str(plan_out)})
