from __future__ import annotations

import shutil
from pathlib import Path

from .utils import run_cmd, truncate


NOISE_ARTIFACT_BASENAMES = {".DS_Store"}
NOISE_ARTIFACT_SUFFIXES = {".pyc", ".pyo"}
NOISE_ARTIFACT_DIRS = {"__pycache__", "build", "dist"}


def _is_egg_info_path(candidate: Path) -> bool:
    return any(part.endswith(".egg-info") for part in candidate.parts)


def is_git_repo(repo: Path) -> bool:
    code, out, _ = run_cmd("git rev-parse --is-inside-work-tree", cwd=repo)
    return code == 0 and out.strip() == "true"


def ensure_git_repo(repo: Path) -> None:
    if not is_git_repo(repo):
        raise SystemExit(f"No es un repo git válido: {repo}")


def git_init(repo: Path) -> None:
    run_cmd(["git", "init"], cwd=repo, check=True)


def git_init_with_initial_commit(repo: Path, message: str = "chore: initialize repository") -> str:
    git_init(repo)
    run_cmd(["git", "add", "-A"], cwd=repo, check=True)
    run_cmd(
        [
            "git",
            "-c",
            "user.name=Molde Maestro",
            "-c",
            "user.email=molde-maestro@local",
            "commit",
            "--allow-empty",
            "-m",
            message,
        ],
        cwd=repo,
        check=True,
    )
    return git_head_commit(repo)


def git_current_branch(repo: Path) -> str:
    _, out, _ = run_cmd("git rev-parse --abbrev-ref HEAD", cwd=repo, check=True)
    return out.strip()


def git_head_commit(repo: Path) -> str:
    _, out, _ = run_cmd("git rev-parse HEAD", cwd=repo, check=True)
    return out.strip()


def git_commit_all(repo: Path, message: str) -> str:
    run_cmd(["git", "add", "-A"], cwd=repo, check=True)
    run_cmd(["git", "commit", "-m", message], cwd=repo, check=True)
    return git_head_commit(repo)


def git_commit_paths(repo: Path, message: str, paths: list[str]) -> str:
    if not paths:
        raise ValueError("git_commit_paths requiere al menos una ruta.")
    run_cmd(["git", "add", "-A", "--", *paths], cwd=repo, check=True)
    run_cmd(["git", "commit", "-m", message], cwd=repo, check=True)
    return git_head_commit(repo)


def git_changed_files_between(repo: Path, base_ref: str, head_ref: str = "HEAD") -> list[str]:
    code, out, _ = run_cmd(["git", "diff", "--name-only", f"{base_ref}..{head_ref}"], cwd=repo)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def git_checkout(repo: Path, branch: str) -> None:
    run_cmd(["git", "checkout", branch], cwd=repo, check=True)


def git_create_branch(repo: Path, branch_name: str) -> None:
    run_cmd(["git", "checkout", "-b", branch_name], cwd=repo, check=True)


def git_switch(repo: Path, branch: str) -> None:
    run_cmd(["git", "switch", branch], cwd=repo, check=True)


def git_switch_or_checkout(repo: Path, branch: str) -> None:
    code, _, _ = run_cmd(["git", "switch", branch], cwd=repo, capture=True)
    if code == 0:
        return
    git_checkout(repo, branch)


def git_create_or_reset_branch(repo: Path, branch_name: str, start_point: str) -> None:
    run_cmd(["git", "checkout", "-B", branch_name, start_point], cwd=repo, check=True)


def git_status_porcelain(repo: Path) -> str:
    _, out, _ = run_cmd("git status --porcelain", cwd=repo)
    return out.strip()


def git_has_working_tree_changes(repo: Path) -> bool:
    return bool(git_status_porcelain(repo))


def is_noise_artifact_path(path: str) -> bool:
    candidate = Path(path)
    if candidate.name in NOISE_ARTIFACT_BASENAMES:
        return True
    if candidate.suffix in NOISE_ARTIFACT_SUFFIXES:
        return True
    if any(part in NOISE_ARTIFACT_DIRS for part in candidate.parts):
        return True
    if _is_egg_info_path(candidate):
        return True
    return False


def git_status_entries(repo: Path) -> list[tuple[str, str]]:
    _, out, _ = run_cmd("git status --porcelain", cwd=repo)
    entries: list[tuple[str, str]] = []
    for raw_line in out.splitlines():
        if not raw_line.strip():
            continue
        status = raw_line[:2]
        path_text = raw_line[3:]
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        entries.append((status, path_text.strip()))
    return entries


def remove_paths(repo: Path, paths: list[str]) -> list[str]:
    removed: list[str] = []
    for rel in paths:
        target = repo / rel
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed.append(rel)
            continue
        if target.exists():
            target.unlink()
            removed.append(rel)
    return removed


def cleanup_untracked_noise_artifacts(repo: Path) -> list[str]:
    untracked_noise = untracked_noise_artifacts(repo)
    return remove_paths(repo, untracked_noise)


def tracked_noise_files(repo: Path) -> list[str]:
    code, out, _ = run_cmd("git ls-files", cwd=repo, capture=True)
    if code != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip() and is_noise_artifact_path(line.strip())]


def untracked_noise_artifacts(repo: Path) -> list[str]:
    return [
        path
        for status, path in git_status_entries(repo)
        if status == "??" and is_noise_artifact_path(path)
    ]


def cleanup_tracked_noise_artifacts(repo: Path, commit_message: str = "chore: remove generated artifacts") -> list[str]:
    tracked_noise = tracked_noise_files(repo)
    if not tracked_noise:
        return []
    run_cmd(["git", "rm", "-f", "--", *tracked_noise], cwd=repo, check=True)
    return tracked_noise


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


def git_current_branch_or_none(repo: Path) -> str | None:
    code, out, _ = run_cmd(["git", "branch", "--show-current"], cwd=repo, capture=True)
    if code != 0:
        return None
    value = out.strip()
    return value or None


def infer_base_ref(repo: Path) -> str:
    code, out, _ = run_cmd("git rev-parse --abbrev-ref --symbolic-full-name @{u}", cwd=repo)
    if code == 0:
        upstream = out.strip()
        code2, out2, _ = run_cmd(f"git merge-base HEAD {shlex.quote(upstream)}", cwd=repo)
        if code2 == 0 and out2.strip():
            return out2.strip()
    if git_ref_exists(repo, "HEAD~1"):
        return "HEAD~1"
    return "HEAD"


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


def collect_working_tree_changed_files(repo: Path) -> list[str]:
    changed: list[str] = []
    seen: set[str] = set()
    for _status, path in git_status_entries(repo):
        if path and path not in seen:
            seen.add(path)
            changed.append(path)
    return changed


def collect_working_tree_diff(repo: Path) -> str:
    chunks: list[str] = []
    for cmd in (["git", "diff", "--cached", "HEAD"], ["git", "diff", "HEAD"]):
        code, out, err = run_cmd(cmd, cwd=repo, capture=True)
        if code == 0 and out.strip():
            chunks.append(out.strip())
        elif code != 0 and err.strip():
            chunks.append(f"[git diff failed]\n{err.strip()}")
    untracked = [path for status, path in git_status_entries(repo) if status == "??"]
    if untracked:
        chunks.append("Untracked files:\n" + "\n".join(untracked))
    return truncate("\n\n".join(chunk for chunk in chunks if chunk).strip(), 20000)


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
