from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class BaseCommandConfig:
    repo: str
    ai_dir: str
    goals: str
    reasoner: str
    _raw_args: Any = dataclasses.field(repr=False, compare=False)


@dataclasses.dataclass(frozen=True)
class SnapshotCommandConfig(BaseCommandConfig):
    zip_enabled: bool
    ref: str
    out: str

    @classmethod
    def from_args(cls, args) -> "SnapshotCommandConfig":
        return cls(
            repo=args.repo,
            ai_dir=args.ai_dir,
            goals=args.goals,
            reasoner=args.reasoner,
            zip_enabled=bool(args.zip),
            ref=args.ref,
            out=args.out,
            _raw_args=args,
        )


@dataclasses.dataclass(frozen=True)
class PlanCommandConfig(BaseCommandConfig):
    plan_out: str
    plan_mode: str
    reasoner_timeout: int
    plan_fallback_reasoner: str
    plan_fallback_timeout: int

    @classmethod
    def from_args(cls, args) -> "PlanCommandConfig":
        return cls(
            repo=args.repo,
            ai_dir=args.ai_dir,
            goals=args.goals,
            reasoner=args.reasoner,
            plan_out=args.plan_out,
            plan_mode=args.plan_mode,
            reasoner_timeout=args.reasoner_timeout,
            plan_fallback_reasoner=args.plan_fallback_reasoner,
            plan_fallback_timeout=args.plan_fallback_timeout,
            _raw_args=args,
        )


@dataclasses.dataclass(frozen=True)
class ApplyCommandConfig(BaseCommandConfig):
    aider_model: str
    plan_mode: str
    base: str
    branch: str
    aider_timeout: int
    aider_extra_args: list[str]
    apply_enforce_plan_scope: bool

    @classmethod
    def from_args(cls, args) -> "ApplyCommandConfig":
        return cls(
            repo=args.repo,
            ai_dir=args.ai_dir,
            goals=args.goals,
            reasoner=args.reasoner,
            aider_model=args.aider_model,
            plan_mode=args.plan_mode,
            base=args.base,
            branch=args.branch,
            aider_timeout=args.aider_timeout,
            aider_extra_args=list(args.aider_extra_arg),
            apply_enforce_plan_scope=bool(args.apply_enforce_plan_scope),
            _raw_args=args,
        )


@dataclasses.dataclass(frozen=True)
class TestCommandConfig(BaseCommandConfig):
    lint_required: bool
    test_timeout: int

    @classmethod
    def from_args(cls, args) -> "TestCommandConfig":
        return cls(
            repo=args.repo,
            ai_dir=args.ai_dir,
            goals=args.goals,
            reasoner=args.reasoner,
            lint_required=bool(args.lint_required),
            test_timeout=args.test_timeout,
            _raw_args=args,
        )


@dataclasses.dataclass(frozen=True)
class ReportCommandConfig(BaseCommandConfig):
    base_ref: str
    report_timeout: int

    @classmethod
    def from_args(cls, args) -> "ReportCommandConfig":
        return cls(
            repo=args.repo,
            ai_dir=args.ai_dir,
            goals=args.goals,
            reasoner=args.reasoner,
            base_ref=args.base_ref,
            report_timeout=args.report_timeout,
            _raw_args=args,
        )


@dataclasses.dataclass(frozen=True)
class RunCommandConfig(BaseCommandConfig):
    aider_model: str
    zip_enabled: bool
    plan_mode: str
    base: str
    branch: str
    max_iters: int
    aider_timeout: int
    test_timeout: int
    report_timeout: int
    lint_required: bool
    aider_extra_args: list[str]
    apply_enforce_plan_scope: bool

    @classmethod
    def from_args(cls, args) -> "RunCommandConfig":
        return cls(
            repo=args.repo,
            ai_dir=args.ai_dir,
            goals=args.goals,
            reasoner=args.reasoner,
            aider_model=args.aider_model,
            zip_enabled=bool(args.zip),
            plan_mode=args.plan_mode,
            base=args.base,
            branch=args.branch,
            max_iters=args.max_iters,
            aider_timeout=args.aider_timeout,
            test_timeout=args.test_timeout,
            report_timeout=args.report_timeout,
            lint_required=bool(args.lint_required),
            aider_extra_args=list(args.aider_extra_arg),
            apply_enforce_plan_scope=bool(args.apply_enforce_plan_scope),
            _raw_args=args,
        )
