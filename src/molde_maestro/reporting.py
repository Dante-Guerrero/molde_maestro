from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import subprocess
import datetime as dt
from pathlib import Path
from typing import Any, Optional, TypedDict

from .utils import ExecutionFailure, ensure_ai_dir, format_duration, iso_now, safe_unlink, safe_write, to_text, truncate


class StageRecord(TypedDict):
    name: str
    start_time: Optional[str]
    end_time: Optional[str]
    duration_seconds: Optional[float]
    status: str
    details: dict[str, Any]


class RunErrorRecord(TypedDict):
    message: str
    details: dict[str, Any]


class RunSummaryRecord(TypedDict, total=False):
    tests_passed: bool
    test_status: str
    validation_profile: str


class RunMetadata(TypedDict):
    command: str
    repo: str
    ai_dir: str
    config_path: Optional[str]
    started_at: str
    completed_at: Optional[str]
    status: str
    stages: list[StageRecord]
    error: Optional[RunErrorRecord]
    summary: RunSummaryRecord


def build_stage_record(name: str) -> StageRecord:
    return {
        "name": name,
        "start_time": None,
        "end_time": None,
        "duration_seconds": None,
        "status": "pending",
        "details": {},
    }


@dataclasses.dataclass
class RunRecorder:
    ai_dir: Path
    command: str
    repo: Path
    config_path: Optional[Path] = None
    metadata_name: str = "run-metadata.json"
    metadata_path: Path = dataclasses.field(init=False)
    metadata: RunMetadata = dataclasses.field(init=False)
    _started_perf: dict[str, float] = dataclasses.field(default_factory=dict, init=False)

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

    def _stage(self, name: str) -> StageRecord:
        for stage in self.metadata["stages"]:
            if stage["name"] == name:
                return stage
        stage = build_stage_record(name)
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

    def finish_stage(self, name: str, status: str, details: Optional[dict[str, Any]] = None) -> None:
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

    def fail_run(self, message: str, status: str = "failed", details: Optional[dict[str, Any]] = None) -> None:
        self.metadata["status"] = status
        self.metadata["completed_at"] = iso_now()
        self.metadata["error"] = {"message": message, "details": details or {}}
        self.save()

    def complete_run(self, status: str = "ok", details: Optional[dict[str, Any]] = None) -> None:
        self.metadata["status"] = status
        self.metadata["completed_at"] = iso_now()
        if details:
            self.metadata["summary"] = details
        self.save()


def init_run_context(args) -> tuple[Path, Path, RunRecorder]:
    repo = Path(args.repo).expanduser().resolve()
    ai_dir = ensure_ai_dir(repo, args.ai_dir)
    cfg_path = getattr(args, "_config_path", None)
    metadata_name = "run-metadata.json"
    if args.cmd == "report" and (ai_dir / "run-metadata.json").exists():
        metadata_name = "report-command-metadata.json"
    recorder = RunRecorder(ai_dir=ai_dir, command=args.cmd, repo=repo, config_path=cfg_path, metadata_name=metadata_name)
    return repo, ai_dir, recorder


def clear_artifacts(ai_dir: Path, names: list[str]) -> None:
    for name in names:
        safe_unlink(ai_dir / name)


def write_stage_error(ai_dir: Path, stage: str, exc: BaseException, extra: Optional[dict[str, Any]] = None) -> Path:
    error_path = ai_dir / f"{stage}-error.md"
    details: dict[str, Any] = {
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
    stage_details: dict[str, Any] = {}
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


def project_reported_metadata(metadata: dict[str, Any], overall_status: Optional[str] = None) -> dict[str, Any]:
    projected = copy.deepcopy(metadata)
    if overall_status:
        projected["status"] = overall_status
    for stage in projected.get("stages", []):
        if stage.get("name") == "report":
            stage["status"] = "ok"
    return projected
