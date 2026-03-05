#!/usr/bin/env python3
"""
pipeline.py — Repo improvement pipeline: plan (reasoner) -> apply (aider) -> test -> report.

Typical usage (Ollama local):
  python pipeline.py run \
    --repo /path/to/repo \
    --goals PROJECT_GOALS.md \
    --reasoner ollama:deepseek-r1 \
    --aider-model ollama:qwen3-coder:14b \
    --test-cmd "pytest -q" \
    --lint-cmd "ruff check . || true" \
    --max-iters 2

If you want a snapshot zip:
  python pipeline.py snapshot --repo /path/to/repo --zip

Subcommands:
  snapshot, plan, apply, test, report, run
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional, Tuple, List


# -----------------------------
# Utilities
# -----------------------------

def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def run_cmd(cmd: str | List[str], cwd: Path | None = None, env: dict | None = None,
            check: bool = False, capture: bool = True, timeout: Optional[int] = None) -> Tuple[int, str, str]:
    """
    Run a shell command. Returns (returncode, stdout, stderr).
    """
    if isinstance(cmd, str):
        args = shlex.split(cmd)
    else:
        args = cmd

    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env={**os.environ, **(env or {})},
        text=True,
        capture_output=capture,
        check=False,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {args}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.returncode, proc.stdout, proc.stderr


def ensure_git_repo(repo: Path) -> None:
    code, out, _ = run_cmd("git rev-parse --is-inside-work-tree", cwd=repo)
    if code != 0 or out.strip() != "true":
        raise SystemExit(f"Not a git repo: {repo}")


def safe_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(content)


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default


def truncate(s: str, max_chars: int) -> str:
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + "\n\n[TRUNCATED]\n"


def repo_tree(repo: Path, max_lines: int = 600) -> str:
    """
    Get a lightweight tree listing, excluding typical junk.
    """
    code, out, err = run_cmd(
        "git ls-files",
        cwd=repo,
        capture=True
    )
    if code != 0:
        return f"[Could not list files]\n{err}"

    files = out.strip().splitlines()
    # basic filtering of common junk even if tracked
    skip_patterns = [
        r"^AI/.*",
        r"^\.git/.*",
        r"^node_modules/.*",
        r"^dist/.*",
        r"^build/.*",
        r"^\.venv/.*",
        r"^venv/.*",
        r"^__pycache__/.*",
    ]
    rx = re.compile("|".join(f"(?:{p})" for p in skip_patterns))
    files = [f for f in files if not rx.match(f)]
    files = files[:max_lines]
    return "\n".join(files)


def head_file(repo: Path, rel: str, max_chars: int = 8000) -> str:
    p = repo / rel
    if not p.exists() or not p.is_file():
        return f"[Missing file: {rel}]"
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[Could not read {rel}: {e}]"
    return truncate(txt, max_chars)


# -----------------------------
# Model interaction (Ollama)
# -----------------------------

@dataclasses.dataclass
class ModelSpec:
    kind: str  # "ollama" or "cmd"
    name: str  # model name or command template


def parse_model_spec(s: str) -> ModelSpec:
    """
    Supported:
      ollama:<model>     -> uses `ollama run <model>` with a prompt
      cmd:<command...>   -> uses an arbitrary command that reads prompt from stdin and prints response
    """
    if s.startswith("ollama:"):
        return ModelSpec(kind="ollama", name=s.split(":", 1)[1].strip())
    if s.startswith("cmd:"):
        return ModelSpec(kind="cmd", name=s.split(":", 1)[1].strip())
    raise ValueError("Model must be specified as ollama:<model> or cmd:<...>")


def call_model(model: ModelSpec, prompt: str, cwd: Optional[Path] = None, timeout: int = 1800) -> str:
    """
    Calls a model and returns its output.
    - ollama:<model>: uses `ollama run <model>`
    - cmd:<...>: executes command and feeds prompt to stdin
    """
    if model.kind == "ollama":
        # ollama run MODEL (reads prompt from stdin)
        args = ["ollama", "run", model.name]
    else:
        args = shlex.split(model.name)

    proc = subprocess.run(
        args,
        input=prompt,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=os.environ.copy(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Model call failed ({proc.returncode}).\n"
            f"STDERR:\n{proc.stderr}\n"
            f"STDOUT:\n{proc.stdout}\n"
        )
    return proc.stdout.strip()


# -----------------------------
# Aider interaction
# -----------------------------

def run_aider(repo: Path, aider_model: str, message: str,
              files: Optional[List[str]] = None,
              log_path: Optional[Path] = None,
              extra_args: Optional[List[str]] = None,
              timeout: int = 7200) -> Tuple[int, str, str]:
    """
    Runs aider in non-interactive mode if possible.
    We prefer:
      aider --model <...> --message "<...>" [--file ...]
    Note: aider CLI options vary across versions. This is the most common pattern.

    If your aider doesn't support --message, use --chat-mode / --prompt flags accordingly.
    """
    cmd = ["aider"]

    # Model spec: aider expects e.g. "ollama/qwen2.5-coder:14b" or "ollama:qwen..."
    # We'll pass exactly what user provides.
    cmd += ["--model", aider_model]

    # Make it less chaotic:
    # - auto commit, and clear message-based command
    cmd += ["--auto-commits"]

    if files:
        for f in files:
            cmd += ["--file", f]

    # Primary instruction
    cmd += ["--message", message]

    if extra_args:
        cmd += extra_args

    # Run
    proc = subprocess.run(
        cmd,
        cwd=str(repo),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=os.environ.copy(),
    )

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Aider run ({now_stamp()})\n\n"
            f"## CMD\n```\n{' '.join(shlex.quote(c) for c in cmd)}\n```\n\n"
            f"## STDOUT\n```\n{proc.stdout}\n```\n\n"
            f"## STDERR\n```\n{proc.stderr}\n```\n",
            encoding="utf-8"
        )
    return proc.returncode, proc.stdout, proc.stderr


# -----------------------------
# Pipeline steps
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


def build_reasoner_prompt(repo: Path, goals_text: str, extra_context_files: List[str],
                          max_tree_lines: int = 600) -> str:
    tree = repo_tree(repo, max_lines=max_tree_lines)

    # Pull some key files if present
    key_candidates = [
        "README.md", "pyproject.toml", "requirements.txt", "package.json",
        "Makefile", "justfile", "setup.cfg", "Cargo.toml", "go.mod",
        ".github/workflows/ci.yml", ".github/workflows/test.yml"
    ]
    # Merge with user-specified extra context files
    selected = []
    for k in key_candidates + extra_context_files:
        if k and k not in selected and (repo / k).exists():
            selected.append(k)

    key_blobs = []
    for k in selected[:12]:
        key_blobs.append(f"\n\n### FILE: {k}\n```text\n{head_file(repo, k)}\n```")

    prompt = f"""\
You are a meticulous senior engineer. You will review a repository and produce a single Markdown document called plan.md
following EXACTLY the required structure below. Do not include anything before the Markdown. Do not wrap it in code fences.

REQUIRED STRUCTURE (copy it and fill it):
{PLAN_TEMPLATE}

CONSTRAINTS:
- Keep changes realistic and incremental.
- Prioritize: correctness, tests, maintainability, clarity.
- Respect the goals in PROJECT_GOALS.md.
- Do NOT propose irrelevant refactors or style-only churn.
- In "Commands to validate", include exact commands that match the repo tooling you see.

INPUTS:

## PROJECT_GOALS.md
{goals_text}

## REPO FILE LIST (git ls-files)
{tree}
{''.join(key_blobs)}

Now output the final Markdown for plan.md only.
"""
    return prompt


def git_current_branch(repo: Path) -> str:
    code, out, _ = run_cmd("git rev-parse --abbrev-ref HEAD", cwd=repo, check=True)
    return out.strip()


def git_create_branch(repo: Path, branch_name: str) -> None:
    run_cmd(f"git checkout -b {shlex.quote(branch_name)}", cwd=repo, check=True)


def git_checkout(repo: Path, branch: str) -> None:
    run_cmd(f"git checkout {shlex.quote(branch)}", cwd=repo, check=True)


def git_status_porcelain(repo: Path) -> str:
    _, out, _ = run_cmd("git status --porcelain", cwd=repo)
    return out.strip()


def git_archive_zip(repo: Path, out_zip: Path, ref: str = "HEAD") -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    # git archive produces a tar/zip without .git
    run_cmd(["git", "archive", "--format=zip", "-o", str(out_zip), ref], cwd=repo, check=True)


def default_aider_instruction(plan_md: str) -> str:
    return f"""\
You are Aider acting as an engineer implementing a prepared change plan.

You MUST follow this plan strictly:
- Read AI/plan.md.
- Implement the changes in order.
- Update or add tests as specified.
- Update docs if needed.
- Do not introduce unrelated refactors.
- Keep commits small and meaningful (auto-commits is enabled).

After completing changes, ensure the repo is left in a runnable state.

PLAN:
{plan_md}
"""


def build_report_prompt(goals_text: str, plan_text: str, test_report_md: str, git_diff: str) -> str:
    return f"""\
You are a strict reviewer. Produce a Markdown report called final.md.
Do not wrap output in code fences.

The report must include:
1) Executive summary (what changed, why)
2) Goals coverage (map each goal -> evidence from changes/tests)
3) Test results interpretation (pass/fail, what it means)
4) Risks and regressions
5) Next steps (concrete)

INPUTS:

## PROJECT_GOALS.md
{goals_text}

## AI/plan.md
{plan_text}

## AI/test-report.md
{test_report_md}

## git diff (against base)
{git_diff}

Now output final.md only.
"""


def collect_git_diff(repo: Path, base_ref: str) -> str:
    code, out, err = run_cmd(f"git diff {shlex.quote(base_ref)}...HEAD", cwd=repo, capture=True)
    if code != 0:
        return f"[git diff failed]\n{err}"
    return truncate(out, 20000)


def write_test_report(repo: Path, ai_dir: Path, lint_cmd: Optional[str], test_cmd: str) -> Tuple[bool, str]:
    """
    Run lint and tests. Save a Markdown report and JSON summary.
    Returns (passed, report_md).
    """
    entries = []
    summary = {"lint": None, "tests": None, "passed": False, "timestamp": now_stamp()}

    def run_and_capture(label: str, cmd: str) -> dict:
        rc, out, err = run_cmd(cmd, cwd=repo, capture=True)
        return {"label": label, "cmd": cmd, "returncode": rc, "stdout": out, "stderr": err}

    if lint_cmd:
        lint_res = run_and_capture("lint", lint_cmd)
        summary["lint"] = {"returncode": lint_res["returncode"]}
        entries.append(lint_res)

    test_res = run_and_capture("tests", test_cmd)
    summary["tests"] = {"returncode": test_res["returncode"]}
    entries.append(test_res)

    passed = (test_res["returncode"] == 0)
    summary["passed"] = passed

    # Markdown report
    md_parts = [f"# Test Report\n\nTimestamp: {summary['timestamp']}\n"]
    for e in entries:
        md_parts.append(f"## {e['label'].title()}\n")
        md_parts.append(f"Command:\n```bash\n{e['cmd']}\n```\n")
        md_parts.append(f"Return code: **{e['returncode']}**\n")
        md_parts.append("STDOUT:\n```text\n" + truncate(e["stdout"], 15000) + "\n```\n")
        md_parts.append("STDERR:\n```text\n" + truncate(e["stderr"], 15000) + "\n```\n")

    report_md = "\n".join(md_parts)
    safe_write(ai_dir / "test-report.md", report_md)
    safe_write(ai_dir / "test-report.json", json.dumps(summary, indent=2))

    return passed, report_md


# -----------------------------
# Main CLI
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Repo improvement pipeline (reasoner -> aider -> tests -> final report).",
        formatter_class=argparse.RawTextHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser):
        sp.add_argument("--repo", required=True, help="Path to git repository")
        sp.add_argument("--goals", default="PROJECT_GOALS.md", help="Goals markdown file path (relative to repo or absolute)")
        sp.add_argument("--ai-dir", default="AI", help="Directory to store AI artifacts (relative to repo)")
        sp.add_argument("--extra-context", action="append", default=[],
                        help="Additional repo files to include in reasoner context (relative paths). Can be repeated.")
        sp.add_argument("--max-tree-lines", type=int, default=600, help="Max lines for git ls-files listing in prompts")

    sp_snap = sub.add_parser("snapshot", help="Create a snapshot zip via git archive")
    add_common(sp_snap)
    sp_snap.add_argument("--zip", action="store_true", help="Actually create a zip snapshot")
    sp_snap.add_argument("--ref", default="HEAD", help="Git ref to archive")
    sp_snap.add_argument("--out", default="", help="Output zip path (default: <repo>/AI/snapshots/<stamp>.zip)")

    sp_plan = sub.add_parser("plan", help="Generate AI/plan.md using the reasoner model")
    add_common(sp_plan)
    sp_plan.add_argument("--reasoner", required=True, help="Reasoner model spec: ollama:<model> or cmd:<...>")
    sp_plan.add_argument("--plan-out", default="", help="Output path for plan.md (default: <repo>/<ai-dir>/plan.md)")

    sp_apply = sub.add_parser("apply", help="Create branch and run aider based on AI/plan.md")
    add_common(sp_apply)
    sp_apply.add_argument("--aider-model", required=True, help="Aider model name (e.g. ollama:qwen3-coder:14b or openai:gpt-4o)")
    sp_apply.add_argument("--branch", default="", help="Branch name (default: ai/<stamp>-improvements)")
    sp_apply.add_argument("--base", default="", help="Base branch/ref to branch from (default: current)")
    sp_apply.add_argument("--allowed-file", action="append", default=[],
                         help="Restrict aider to these files (repeatable). If omitted, aider can touch anything.")
    sp_apply.add_argument("--aider-extra-arg", action="append", default=[],
                         help="Extra args passed to aider (repeatable).")

    sp_test = sub.add_parser("test", help="Run lint/tests and save AI/test-report.*")
    add_common(sp_test)
    sp_test.add_argument("--test-cmd", required=True, help="Command to run tests (quoted string)")
    sp_test.add_argument("--lint-cmd", default="", help="Optional lint command")

    sp_report = sub.add_parser("report", help="Generate AI/final.md from goals/plan/test report and git diff")
    add_common(sp_report)
    sp_report.add_argument("--reasoner", required=True, help="Reasoner model spec: ollama:<model> or cmd:<...>")
    sp_report.add_argument("--base-ref", default="", help="Base ref for diff (default: merge-base with current branch upstream, else HEAD~1)")

    sp_run = sub.add_parser("run", help="Run full pipeline: snapshot(optional) -> plan -> apply -> test -> report")
    add_common(sp_run)
    sp_run.add_argument("--reasoner", required=True, help="Reasoner model spec: ollama:<model> or cmd:<...>")
    sp_run.add_argument("--aider-model", required=True, help="Aider model name (passed to aider --model)")
    sp_run.add_argument("--test-cmd", required=True, help="Command to run tests")
    sp_run.add_argument("--lint-cmd", default="", help="Optional lint command")
    sp_run.add_argument("--zip", action="store_true", help="Create snapshot zip at start")
    sp_run.add_argument("--max-iters", type=int, default=2, help="Max iterations if tests fail (re-run aider with errors)")
    sp_run.add_argument("--branch", default="", help="Branch name (default: ai/<stamp>-improvements)")
    sp_run.add_argument("--allowed-file", action="append", default=[],
                        help="Restrict aider to these files (repeatable).")
    sp_run.add_argument("--aider-extra-arg", action="append", default=[],
                        help="Extra args passed to aider (repeatable).")

    return p


def resolve_goals_path(repo: Path, goals_arg: str) -> Path:
    gp = Path(goals_arg)
    if gp.is_absolute():
        return gp
    return repo / gp


def ensure_ai_dir(repo: Path, ai_dir_name: str) -> Path:
    p = repo / ai_dir_name
    p.mkdir(parents=True, exist_ok=True)
    return p


def cmd_snapshot(args) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    ai_dir = ensure_ai_dir(repo, args.ai_dir)

    if not args.zip:
        print("snapshot: --zip not set; skipping zip creation.")
        return

    out_zip = Path(args.out).expanduser()
    if not args.out:
        out_zip = ai_dir / "snapshots" / f"{now_stamp()}.zip"
    elif not out_zip.is_absolute():
        out_zip = repo / out_zip

    git_archive_zip(repo, out_zip, ref=args.ref)
    print(f"snapshot: wrote {out_zip}")


def cmd_plan(args) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    ai_dir = ensure_ai_dir(repo, args.ai_dir)

    goals_path = resolve_goals_path(repo, args.goals)
    goals_text = read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n")

    reasoner = parse_model_spec(args.reasoner)

    prompt = build_reasoner_prompt(
        repo=repo,
        goals_text=goals_text,
        extra_context_files=args.extra_context,
        max_tree_lines=args.max_tree_lines
    )
    plan_md = call_model(reasoner, prompt, cwd=repo)
    if not plan_md.strip().startswith("#"):
        # fail-safe: enforce markdown-ish output
        plan_md = "# Repo Review Plan\n\n" + plan_md

    plan_out = Path(args.plan_out).expanduser()
    if not args.plan_out:
        plan_out = ai_dir / "plan.md"
    elif not plan_out.is_absolute():
        plan_out = repo / plan_out

    safe_write(plan_out, plan_md)
    print(f"plan: wrote {plan_out}")


def cmd_apply(args) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    ai_dir = ensure_ai_dir(repo, args.ai_dir)

    base = args.base.strip() or git_current_branch(repo)
    branch = args.branch.strip() or f"ai/{now_stamp()}-improvements"

    # Checkout base, create branch
    git_checkout(repo, base)
    git_create_branch(repo, branch)

    plan_path = ai_dir / "plan.md"
    plan_text = read_text(plan_path, default="")
    if not plan_text.strip():
        raise SystemExit(f"apply: missing or empty {plan_path}. Run `plan` first.")

    aider_msg = default_aider_instruction(plan_text)

    log_path = ai_dir / "aider-log.txt"
    rc, out, err = run_aider(
        repo=repo,
        aider_model=args.aider_model,
        message=aider_msg,
        files=args.allowed_file if args.allowed_file else None,
        log_path=log_path,
        extra_args=args.aider_extra_arg,
    )
    print(f"apply: aider return code = {rc}")
    if rc != 0:
        print("apply: aider stderr (truncated):")
        print(truncate(err, 4000))
        raise SystemExit(rc)

    # Basic sanity
    dirty = git_status_porcelain(repo)
    if dirty:
        print("apply: repo has uncommitted changes (aider should have auto-committed).")
        print(dirty)


def cmd_test(args) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    ai_dir = ensure_ai_dir(repo, args.ai_dir)

    lint_cmd = args.lint_cmd.strip() or None
    passed, _ = write_test_report(repo, ai_dir, lint_cmd, args.test_cmd)
    print(f"test: passed={passed} (report in {ai_dir / 'test-report.md'})")
    if not passed:
        raise SystemExit(2)


def infer_base_ref(repo: Path) -> str:
    """
    Try to infer a reasonable base ref for diffs.
    - Prefer merge-base with upstream if exists.
    - Else fallback to HEAD~1.
    """
    # try upstream
    code, out, _ = run_cmd("git rev-parse --abbrev-ref --symbolic-full-name @{u}", cwd=repo)
    if code == 0:
        upstream = out.strip()
        code2, out2, _ = run_cmd(f"git merge-base HEAD {shlex.quote(upstream)}", cwd=repo)
        if code2 == 0 and out2.strip():
            return out2.strip()
    return "HEAD~1"


def cmd_report(args) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    ai_dir = ensure_ai_dir(repo, args.ai_dir)

    goals_path = resolve_goals_path(repo, args.goals)
    goals_text = read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n")

    plan_text = read_text(ai_dir / "plan.md", default="")
    test_report_md = read_text(ai_dir / "test-report.md", default="[Missing test report]")
    if not plan_text.strip():
        raise SystemExit("report: missing AI/plan.md")
    reasoner = parse_model_spec(args.reasoner)

    base_ref = args.base_ref.strip() or infer_base_ref(repo)
    git_diff = collect_git_diff(repo, base_ref)

    prompt = build_report_prompt(goals_text, plan_text, test_report_md, git_diff)
    final_md = call_model(reasoner, prompt, cwd=repo)
    if not final_md.strip().startswith("#"):
        final_md = "# Final Report\n\n" + final_md

    safe_write(ai_dir / "final.md", final_md)
    print(f"report: wrote {ai_dir / 'final.md'} (base_ref={base_ref})")


def cmd_run(args) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    ai_dir = ensure_ai_dir(repo, args.ai_dir)

    # Optional snapshot
    if args.zip:
        out_zip = ai_dir / "snapshots" / f"{now_stamp()}.zip"
        git_archive_zip(repo, out_zip)
        print(f"run: snapshot zip -> {out_zip}")

    # Plan
    goals_path = resolve_goals_path(repo, args.goals)
    goals_text = read_text(goals_path, default="# PROJECT_GOALS.md\n\n[Missing goals file]\n")
    reasoner = parse_model_spec(args.reasoner)

    plan_prompt = build_reasoner_prompt(
        repo=repo,
        goals_text=goals_text,
        extra_context_files=args.extra_context,
        max_tree_lines=args.max_tree_lines
    )
    plan_md = call_model(reasoner, plan_prompt, cwd=repo)
    if not plan_md.strip().startswith("#"):
        plan_md = "# Repo Review Plan\n\n" + plan_md
    safe_write(ai_dir / "plan.md", plan_md)
    print(f"run: plan -> {ai_dir / 'plan.md'}")

    # Apply (branch + aider)
    base = git_current_branch(repo)
    branch = args.branch.strip() or f"ai/{now_stamp()}-improvements"
    git_checkout(repo, base)
    git_create_branch(repo, branch)
    print(f"run: branch -> {branch} (from {base})")

    aider_log = ai_dir / "aider-log.txt"
    aider_msg = default_aider_instruction(plan_md)

    # Iterative loop if tests fail
    lint_cmd = args.lint_cmd.strip() or None
    test_cmd = args.test_cmd

    for i in range(1, args.max_iters + 1):
        rc, _, err = run_aider(
            repo=repo,
            aider_model=args.aider_model,
            message=aider_msg if i == 1 else aider_msg + "\n\nAlso fix the following test/lint failures:\n" + truncate(err, 12000),
            files=args.allowed_file if args.allowed_file else None,
            log_path=aider_log,
            extra_args=args.aider_extra_arg,
        )
        if rc != 0:
            print(f"run: aider failed (iter {i}) rc={rc}")
            print(truncate(err, 4000))
            raise SystemExit(rc)

        passed, test_report_md = write_test_report(repo, ai_dir, lint_cmd, test_cmd)
        print(f"run: iter {i} tests passed={passed}")
        if passed:
            break

        # Feed back failures into next iteration
        err = test_report_md

        if i == args.max_iters:
            print("run: reached max iterations with failing tests.")
            # continue to report anyway

    # Report
    base_ref = infer_base_ref(repo)
    git_diff = collect_git_diff(repo, base_ref)
    report_prompt = build_report_prompt(goals_text, plan_md, read_text(ai_dir / "test-report.md"), git_diff)
    final_md = call_model(reasoner, report_prompt, cwd=repo)
    if not final_md.strip().startswith("#"):
        final_md = "# Final Report\n\n" + final_md
    safe_write(ai_dir / "final.md", final_md)
    print(f"run: final -> {ai_dir / 'final.md'} (base_ref={base_ref})")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.cmd == "snapshot":
            cmd_snapshot(args)
        elif args.cmd == "plan":
            cmd_plan(args)
        elif args.cmd == "apply":
            cmd_apply(args)
        elif args.cmd == "test":
            cmd_test(args)
        elif args.cmd == "report":
            cmd_report(args)
        elif args.cmd == "run":
            cmd_run(args)
        else:
            raise SystemExit("Unknown command")
    except subprocess.TimeoutExpired:
        raise SystemExit("Timed out running a command (model/aider/tests).")
    except RuntimeError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()