from __future__ import annotations

import shlex
from pathlib import Path

from .utils import run_cmd, truncate


def ensure_git_repo(repo: Path) -> None:
    code, out, _ = run_cmd("git rev-parse --is-inside-work-tree", cwd=repo)
    if code != 0 or out.strip() != "true":
        raise SystemExit(f"No es un repo git válido: {repo}")


def git_current_branch(repo: Path) -> str:
    _, out, _ = run_cmd("git rev-parse --abbrev-ref HEAD", cwd=repo, check=True)
    return out.strip()


def git_head_commit(repo: Path) -> str:
    _, out, _ = run_cmd("git rev-parse HEAD", cwd=repo, check=True)
    return out.strip()


def git_changed_files_between(repo: Path, base_ref: str, head_ref: str = "HEAD") -> list[str]:
    code, out, _ = run_cmd(["git", "diff", "--name-only", f"{base_ref}..{head_ref}"], cwd=repo)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def git_checkout(repo: Path, branch: str) -> None:
    run_cmd(f"git checkout {shlex.quote(branch)}", cwd=repo, check=True)


def git_create_branch(repo: Path, branch_name: str) -> None:
    run_cmd(f"git checkout -b {shlex.quote(branch_name)}", cwd=repo, check=True)


def git_status_porcelain(repo: Path) -> str:
    _, out, _ = run_cmd("git status --porcelain", cwd=repo)
    return out.strip()


def git_ref_exists(repo: Path, ref: str) -> bool:
    code, _, _ = run_cmd(["git", "rev-parse", "--verify", ref], cwd=repo)
    return code == 0


def git_archive_zip(repo: Path, out_zip: Path, ref: str = "HEAD") -> None:
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    run_cmd(["git", "archive", "--format=zip", "-o", str(out_zip), ref], cwd=repo, check=True)


def resolve_base_branch(repo: Path, requested_base: str = "") -> str:
    requested_base = (requested_base or "").strip()
    if requested_base:
        if not git_ref_exists(repo, requested_base):
            raise SystemExit(f"La rama/ref base no existe: {requested_base}")
        return requested_base

    current = git_current_branch(repo)
    if current.startswith("ai/"):
        for candidate in ("main", "master"):
            if git_ref_exists(repo, candidate):
                return candidate
    return current


def infer_base_ref(repo: Path) -> str:
    code, out, _ = run_cmd("git rev-parse --abbrev-ref --symbolic-full-name @{u}", cwd=repo)
    if code == 0:
        upstream = out.strip()
        code2, out2, _ = run_cmd(f"git merge-base HEAD {shlex.quote(upstream)}", cwd=repo)
        if code2 == 0 and out2.strip():
            return out2.strip()
    return "HEAD~1"


def collect_git_diff(repo: Path, base_ref: str) -> str:
    code, out, err = run_cmd(f"git diff {shlex.quote(base_ref)}...HEAD", cwd=repo, capture=True)
    if code != 0:
        return f"[git diff failed]\n{err}"
    return truncate(out, 20000)


def collect_git_changed_files(repo: Path, base_ref: str) -> list[str]:
    code, out, _ = run_cmd(["git", "diff", "--name-only", f"{base_ref}...HEAD"], cwd=repo, capture=True)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def infer_validation_changed_files(repo: Path, args) -> tuple[list[str], str]:
    base_ref = (getattr(args, "base_ref", "") or "").strip()
    if not base_ref:
        try:
            base_ref = infer_base_ref(repo)
        except Exception:
            base_ref = "HEAD~1"
    changed = collect_git_changed_files(repo, base_ref)
    if changed:
        return changed, base_ref
    code, out, _ = run_cmd(["git", "diff", "--name-only", "HEAD"], cwd=repo, capture=True)
    if code == 0:
        fallback = [line.strip() for line in out.splitlines() if line.strip()]
        if fallback:
            return fallback, "HEAD"
    return [], base_ref
