from __future__ import annotations

import ast
import builtins
import dataclasses
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, TypedDict

from .git_ops import collect_git_changed_files, infer_base_ref
from .utils import command_exists, looks_like_shell_command, now_stamp, read_text, run_cmd, safe_write, to_text, truncate


@dataclasses.dataclass
class FunctionSignature:
    name: str
    required_positional: int
    max_positional: Optional[int]
    keyword_only_required: list[str]


@dataclasses.dataclass
class ValidationContext:
    is_python_repo: bool
    has_tests: bool
    has_pyproject: bool
    has_requirements: bool
    has_src_dir: bool
    has_main_py: bool
    has_dot_venv: bool
    has_uv_lock: bool
    has_setup_cfg: bool
    python_runner: Optional[str]
    python_runner_source: str
    runner_reproducible: bool
    runner_usable: bool
    pytest_available: bool
    pytest_command: str
    compile_command: str
    repo_notes: list[str]
    suggested_profile: str
    suggestion_reason: str
    promotion_reasons: list[str]
    promotion_blockers: list[str]


@dataclasses.dataclass
class ValidationPlan:
    profile: str
    source: str
    suggested_profile: str
    suggestion_reason: str
    promotion_decision: str
    promotion_reasons: list[str]
    promotion_blockers: list[str]
    test_command: str
    lint_command: Optional[str]
    semantic_validation: bool
    semantic_validation_mode: str
    semantic_validation_strict: bool
    semantic_validation_timeout: int
    smoke_imports: bool
    smoke_imports_strict: bool
    smoke_import_runner: Optional[str]
    validation_evidence_level: str
    checks: list[dict[str, Any]]
    context: ValidationContext


class ValidationCheck(TypedDict, total=False):
    name: str
    level: int
    status: str
    blocking: bool
    command: str


class CommandExecutionResult(TypedDict):
    label: str
    cmd: str
    returncode: Optional[int]
    stdout: str
    stderr: str
    use_shell: bool
    status: str
    timeout_seconds: Optional[int]


class TestReportSummary(TypedDict):
    validation_profile: Optional[dict[str, Any]]
    lint: Optional[dict[str, Any]]
    tests: Optional[dict[str, Any]]
    lint_required: bool
    passed: bool
    timestamp: str
    semantic_validation: dict[str, Any]
    smoke_imports: dict[str, Any]
    checks: list[ValidationCheck]
    status: str
    validation_evidence_level: str


def build_validation_check(
    name: str,
    level: int,
    status: str,
    blocking: bool,
    *,
    command: Optional[str] = None,
) -> ValidationCheck:
    check: ValidationCheck = {
        "name": name,
        "level": level,
        "status": status,
        "blocking": blocking,
    }
    if command:
        check["command"] = command
    return check


def build_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> FunctionSignature:
    positional = len(node.args.posonlyargs) + len(node.args.args)
    defaults = len(node.args.defaults)
    required_positional = max(0, positional - defaults)
    max_positional = None if node.args.vararg else positional
    keyword_only_required = [
        arg.arg for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is None
    ]
    return FunctionSignature(
        name=node.name,
        required_positional=required_positional,
        max_positional=max_positional,
        keyword_only_required=keyword_only_required,
    )


def collect_local_function_signatures(tree: ast.AST) -> dict[str, FunctionSignature]:
    signatures: dict[str, FunctionSignature] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signatures[node.name] = build_function_signature(node)
    return signatures


def module_path_from_import(repo: Path, current_file: Path, module: Optional[str], level: int) -> Optional[Path]:
    try:
        rel_parts = current_file.relative_to(repo).parts
    except ValueError:
        return None
    if level > 0:
        package_parts = list(rel_parts[:-1])
        if level > len(package_parts):
            return None
        package_parts = package_parts[: len(package_parts) - level + 1]
        if module:
            package_parts.extend(module.split("."))
        candidate = repo.joinpath(*package_parts)
    else:
        if not module:
            return None
        candidate = repo.joinpath(*module.split("."))
    py_candidate = candidate.with_suffix(".py")
    init_candidate = candidate / "__init__.py"
    if py_candidate.exists():
        return py_candidate
    if init_candidate.exists():
        return init_candidate
    return None


def resolve_imported_local_signatures(repo: Path, rel_path: str, tree: ast.AST) -> tuple[dict[str, FunctionSignature], list[dict[str, Any]], set[str]]:
    signatures: dict[str, FunctionSignature] = {}
    import_issues: list[dict[str, Any]] = []
    imported_names: set[str] = set()
    file_path = repo / rel_path
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_names.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
            module_path = module_path_from_import(repo, file_path, node.module, node.level)
            if not module_path:
                if node.level > 0:
                    import_issues.append(
                        {
                            "kind": "local_import_unresolved",
                            "severity": "failed",
                            "file": rel_path,
                            "message": f"No se pudo resolver el import local: from {'.' * node.level}{node.module or ''} import ...",
                        }
                    )
                continue
            source = read_text(module_path)
            try:
                imported_tree = ast.parse(source)
            except SyntaxError as exc:
                import_issues.append(
                    {
                        "kind": "local_import_syntax_error",
                        "severity": "failed",
                        "file": rel_path,
                        "message": f"El módulo importado {module_path.relative_to(repo)} no parsea: {exc}",
                    }
                )
                continue
            imported_signatures = collect_local_function_signatures(imported_tree)
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name in imported_signatures:
                    signatures[alias.asname or alias.name] = imported_signatures[alias.name]
    return signatures, import_issues, imported_names


def run_optional_semantic_tool(repo: Path, files: list[str], mode: str, timeout: int) -> dict[str, Any]:
    tool_cmd: Optional[list[str]] = None
    tool_name = ""
    if mode in {"auto", "ruff"} and command_exists("ruff"):
        tool_name = "ruff"
        tool_cmd = ["ruff", "check", "--select", "F821,F822,F823", *files]
    elif mode in {"auto", "pyflakes"} and command_exists("pyflakes"):
        tool_name = "pyflakes"
        tool_cmd = ["pyflakes", *files]
    else:
        return {"tool": None, "status": "skipped", "issues": [], "message": "No optional semantic tool available."}

    try:
        rc, out, err = run_cmd(tool_cmd, cwd=repo, capture=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"tool": tool_name, "status": "warning", "issues": [], "message": f"{tool_name} timed out after {timeout}s."}
    output = "\n".join(part for part in [out.strip(), err.strip()] if part).strip()
    if rc == 0:
        return {"tool": tool_name, "status": "ok", "issues": [], "message": output}
    return {
        "tool": tool_name,
        "status": "failed",
        "issues": [{"kind": "optional_tool", "severity": "failed", "message": output or f"{tool_name} reported issues."}],
        "message": output,
    }


def python_module_name_for_path(rel_path: str) -> Optional[str]:
    path = Path(rel_path)
    if path.suffix != ".py":
        return None
    parts = list(path.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def run_python_smoke_imports(repo: Path, changed_files: list[str], runner: Optional[str], timeout: int) -> dict[str, Any]:
    if not runner:
        return {"status": "skipped", "issues": [], "warnings": ["No Python runner available for smoke imports."], "modules": []}
    modules = [python_module_name_for_path(path) for path in changed_files if path.endswith(".py")]
    modules = [module for module in modules if module]
    if not modules:
        return {"status": "skipped", "issues": [], "warnings": ["No changed Python modules to import."], "modules": []}
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(repo), str(repo / "src"), env.get("PYTHONPATH", "")]).strip(os.pathsep)
    issues: list[dict[str, Any]] = []
    for module in modules:
        try:
            rc, out, err = run_cmd([runner, "-c", f"import importlib; importlib.import_module('{module}')"], cwd=repo, capture=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            return {"status": "warning", "issues": [], "warnings": [f"Smoke import timed out after {timeout}s."], "modules": modules}
        if rc != 0:
            issues.append(
                {
                    "kind": "smoke_import_failed",
                    "severity": "failed",
                    "module": module,
                    "message": truncate((out + "\n" + err).strip(), 2000),
                }
            )
    return {"status": "failed" if issues else "ok", "issues": issues, "warnings": [], "modules": modules}


def run_semantic_validation(
    repo: Path,
    changed_files: list[str],
    *,
    enabled: bool,
    mode: str,
    strict: bool,
    timeout: int,
    audit_python_dependency_declarations,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "enabled": enabled,
        "mode": mode,
        "strict": strict,
        "timeout_seconds": timeout,
        "status": "skipped",
        "files_checked": [],
        "issues": [],
        "warnings": [],
        "tools": [],
    }
    if not enabled:
        summary["warnings"].append("Semantic validation disabled.")
        return summary

    python_files = [path for path in changed_files if path.endswith(".py")]
    summary["files_checked"] = python_files
    if not python_files:
        summary["warnings"].append("No changed Python files to validate.")
        return summary

    for rel_path in python_files:
        source = read_text(repo / rel_path)
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            summary["issues"].append({"kind": "syntax_error", "severity": "failed", "file": rel_path, "message": str(exc)})
            continue

        local_sigs = collect_local_function_signatures(tree)
        imported_sigs, import_issues, imported_names = resolve_imported_local_signatures(repo, rel_path, tree)
        summary["issues"].extend(import_issues)
        known_callables = {**local_sigs, **imported_sigs}
        module_names = set(local_sigs) | imported_names | set(dir(builtins))

        class SemanticVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scopes: list[set[str]] = [set(module_names)]

            def _all_names(self) -> set[str]:
                combined: set[str] = set()
                for scope in self.scopes:
                    combined.update(scope)
                return combined

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                params = {arg.arg for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}
                if node.args.vararg:
                    params.add(node.args.vararg.arg)
                if node.args.kwarg:
                    params.add(node.args.kwarg.arg)
                self.scopes.append(params)
                self.generic_visit(node)
                self.scopes.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Assign(self, node: ast.Assign) -> None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.scopes[-1].add(target.id)
                self.generic_visit(node)

            def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
                if isinstance(node.target, ast.Name):
                    self.scopes[-1].add(node.target.id)
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                if isinstance(node.func, ast.Name):
                    callee = node.func.id
                    if any(keyword.arg is None for keyword in node.keywords):
                        return
                    if callee in known_callables:
                        sig = known_callables[callee]
                        provided_positional = len(node.args)
                        provided_keyword_names = {kw.arg for kw in node.keywords if kw.arg}
                        missing_required = max(0, sig.required_positional - provided_positional - len([name for name in provided_keyword_names if name]))
                        if sig.max_positional is not None and provided_positional > sig.max_positional:
                            summary["issues"].append(
                                {
                                    "kind": "call_arity_mismatch",
                                    "severity": "failed",
                                    "file": rel_path,
                                    "message": f"Llamada a {callee} con demasiados argumentos posicionales ({provided_positional}); máximo {sig.max_positional}.",
                                    "line": node.lineno,
                                }
                            )
                        elif missing_required > 0:
                            summary["issues"].append(
                                {
                                    "kind": "call_arity_mismatch",
                                    "severity": "failed",
                                    "file": rel_path,
                                    "message": f"Llamada a {callee} con argumentos faltantes; requiere {sig.required_positional} posicionales.",
                                    "line": node.lineno,
                                }
                            )
                    elif callee not in self._all_names():
                        summary["issues"].append(
                            {
                                "kind": "undefined_call_target",
                                "severity": "failed",
                                "file": rel_path,
                                "message": f"Llamada a nombre no definido: {callee}",
                                "line": node.lineno,
                            }
                        )
                self.generic_visit(node)

        SemanticVisitor().visit(tree)

    dependency_audit = audit_python_dependency_declarations(repo, python_files)
    if not dependency_audit["ok"]:
        for issue in dependency_audit["issues"]:
            summary["issues"].append(
                {
                    "kind": "undeclared_dependency",
                    "severity": "failed",
                    "file": issue["file"],
                    "message": f"Imports sin dependencia declarada: {', '.join(issue['missing_dependencies'])}",
                }
            )

    optional_tool = run_optional_semantic_tool(repo, python_files, mode, timeout)
    summary["tools"].append(optional_tool)
    if optional_tool["status"] == "warning" and optional_tool.get("message"):
        summary["warnings"].append(optional_tool["message"])
    summary["issues"].extend(optional_tool.get("issues", []))

    failed_issues = [issue for issue in summary["issues"] if issue.get("severity") == "failed"]
    if failed_issues:
        summary["status"] = "failed" if strict else "warning"
    elif summary["warnings"]:
        summary["status"] = "warning"
    else:
        summary["status"] = "ok"
    return summary


def detect_validation_commands(repo: Path, args, resolve_validation_plan) -> list[str]:
    commands: list[str] = []
    for candidate in [getattr(args, "lint_cmd", ""), getattr(args, "test_cmd", "")]:
        if str(candidate or "").strip():
            commands.append(str(candidate).strip())
    if commands:
        return commands

    try:
        validation_plan = resolve_validation_plan(repo, args)
        if validation_plan.lint_command:
            commands.append(validation_plan.lint_command)
        if validation_plan.test_command:
            commands.append(validation_plan.test_command)
        if commands:
            return commands[:4]
    except Exception:
        pass

    if (repo / "tests").exists() or (repo / "pytest.ini").exists():
        commands.append("pytest -q")
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        compile_parts = []
        if (repo / "src").exists():
            compile_parts.append("src")
        if (repo / "main.py").exists():
            compile_parts.append("main.py")
        if compile_parts:
            commands.append(f"python3 -m compileall {' '.join(compile_parts)}")
    if (repo / "package.json").exists():
        commands.append("npm test")
    if (repo / "Cargo.toml").exists():
        commands.append("cargo test")
    if (repo / "go.mod").exists():
        commands.append("go test ./...")
    return commands[:4]


def repo_python_runner(repo: Path) -> tuple[Optional[str], str]:
    candidates = [
        (repo / ".venv" / "bin" / "python", ".venv"),
        (repo / ".venv" / "Scripts" / "python.exe", ".venv"),
    ]
    for path, source in candidates:
        if path.exists():
            return str(path), source
    if command_exists("python3"):
        return "python3", "system"
    if command_exists("python"):
        return "python", "system"
    return None, "missing"


def python_module_available(runner: Optional[str], module: str, repo: Path, timeout: int = 10) -> bool:
    if not runner:
        return False
    try:
        rc, _, _ = run_cmd([runner, "-c", f"import {module}"], cwd=repo, capture=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return rc == 0


def runner_executes_python(runner: Optional[str], repo: Path, timeout: int = 10) -> bool:
    if not runner:
        return False
    try:
        rc, _, _ = run_cmd([runner, "-c", "print('ok')"], cwd=repo, capture=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return rc == 0


def detect_validation_context(
    repo: Path,
    *,
    runner_executes_python_fn=runner_executes_python,
    python_module_available_fn=python_module_available,
) -> ValidationContext:
    has_pyproject = (repo / "pyproject.toml").exists()
    has_requirements = (repo / "requirements.txt").exists()
    has_setup_cfg = (repo / "setup.cfg").exists()
    has_src_dir = (repo / "src").exists()
    has_main_py = (repo / "main.py").exists()
    has_tests = (repo / "tests").exists() or (repo / "pytest.ini").exists()
    has_dot_venv = (repo / ".venv").exists()
    has_uv_lock = (repo / "uv.lock").exists()
    is_python_repo = any([has_pyproject, has_requirements, has_setup_cfg, has_src_dir, has_main_py])
    runner, runner_source = repo_python_runner(repo)
    runner_reproducible = runner_source == ".venv"
    runner_usable = runner_executes_python_fn(runner, repo) if runner else False
    pytest_available = python_module_available_fn(runner, "pytest", repo) if runner else False
    repo_notes: list[str] = []
    if has_dot_venv:
        repo_notes.append("Detected .venv in target repo.")
    if has_uv_lock:
        repo_notes.append("Detected uv.lock in target repo.")
        repo_notes.append("uv.lock is only supporting evidence; it is not used as an execution runner by itself.")
    if not has_dot_venv:
        repo_notes.append("No repo-local virtual environment detected.")
    if runner and not runner_usable:
        repo_notes.append("Detected Python runner did not execute successfully.")
    if has_tests and not pytest_available:
        repo_notes.append("Tests exist but pytest is not available in the detected Python runner.")
    compile_target = []
    if has_src_dir:
        compile_target.append("src")
    if has_main_py:
        compile_target.append("main.py")
    compile_command = f"{runner} -m compileall {' '.join(compile_target)}".strip() if runner and compile_target else ""
    pytest_command = f"{runner} -m pytest -q" if has_tests and pytest_available and runner else ""
    promotion_reasons: list[str] = []
    promotion_blockers: list[str] = []
    if is_python_repo:
        if runner_reproducible:
            promotion_reasons.append("Repo-local Python runner detected in .venv.")
        else:
            promotion_blockers.append("No reproducible repo-local Python runner was found.")
        if runner_usable:
            promotion_reasons.append("The selected Python runner executes successfully.")
        else:
            promotion_blockers.append("The selected Python runner is not executable.")
        if compile_command:
            promotion_reasons.append("The runner can execute a repo-scoped compile command.")
        else:
            promotion_blockers.append("No repo-scoped Python validation command could be derived.")
        if has_tests:
            if pytest_command:
                promotion_reasons.append("pytest is available inside the selected runner.")
            else:
                repo_notes.append("pytest-based repo validation is not available; promotion may still use smoke imports only.")
        else:
            repo_notes.append("Repo has no test suite; python_repo, if promoted, will rely on smoke imports.")
    if is_python_repo and runner_reproducible and runner_usable and compile_command:
        suggested_profile = "python_repo"
        suggestion_reason = "Python repo with a reproducible local runner and repo-scoped validation commands."
    elif is_python_repo:
        suggested_profile = "python_local"
        suggestion_reason = "Python repo without enough reproducible environment evidence; favor localized checks."
    else:
        suggested_profile = "minimal"
        suggestion_reason = "No clear Python repo structure detected."
    return ValidationContext(
        is_python_repo=is_python_repo,
        has_tests=has_tests,
        has_pyproject=has_pyproject,
        has_requirements=has_requirements,
        has_setup_cfg=has_setup_cfg,
        has_src_dir=has_src_dir,
        has_main_py=has_main_py,
        has_dot_venv=has_dot_venv,
        has_uv_lock=has_uv_lock,
        python_runner=runner,
        python_runner_source=runner_source,
        runner_reproducible=runner_reproducible,
        runner_usable=runner_usable,
        pytest_available=pytest_available,
        pytest_command=pytest_command,
        compile_command=compile_command,
        repo_notes=repo_notes,
        suggested_profile=suggested_profile,
        suggestion_reason=suggestion_reason,
        promotion_reasons=promotion_reasons,
        promotion_blockers=promotion_blockers,
    )


def resolve_validation_plan(repo: Path, args, *, detect_validation_context_fn=detect_validation_context) -> ValidationPlan:
    context = detect_validation_context_fn(repo)
    requested = (getattr(args, "validation_profile", "") or "auto").strip().lower()
    if requested not in {"auto", "minimal", "python_local", "python_repo", "strict"}:
        raise SystemExit(f"validation_profile inválido: {requested}.")
    if requested == "auto":
        profile = context.suggested_profile
        source = "auto"
    else:
        profile = requested
        source = "explicit"
    promotion_decision = "promoted" if profile == "python_repo" and context.suggested_profile == "python_repo" else "not_promoted"
    if requested == "strict" and context.suggested_profile == "python_repo":
        promotion_decision = "promoted"
    if requested in {"python_repo", "strict"} and context.suggested_profile != "python_repo":
        promotion_decision = "forced"

    explicit_test_cmd = str(getattr(args, "test_cmd", "") or "").strip()
    explicit_lint_cmd = str(getattr(args, "lint_cmd", "") or "").strip() or None
    test_command = explicit_test_cmd
    if not test_command:
        if profile in {"python_repo", "strict"} and context.pytest_command:
            test_command = context.pytest_command
        elif profile in {"python_local", "python_repo", "strict"} and context.compile_command:
            test_command = context.compile_command

    semantic_enabled = bool(getattr(args, "semantic_validation", False)) or profile in {"python_local", "python_repo", "strict"}
    semantic_mode = getattr(args, "semantic_validation_mode", "auto") or "auto"
    semantic_strict = bool(getattr(args, "semantic_validation_strict", False)) or profile in {"python_local", "python_repo", "strict"}
    semantic_timeout = int(getattr(args, "semantic_validation_timeout", 60) or 60)
    smoke_imports = profile in {"python_repo", "strict"} and context.runner_reproducible and context.runner_usable and bool(context.python_runner)
    smoke_imports_strict = profile == "strict"
    if "pytest" in test_command or "unittest" in test_command:
        validation_evidence_level = "functional_tests"
    elif "compileall" in test_command and (semantic_enabled or smoke_imports):
        validation_evidence_level = "mixed"
    elif "compileall" in test_command:
        validation_evidence_level = "compile_only"
    elif semantic_enabled:
        validation_evidence_level = "semantic_only"
    else:
        validation_evidence_level = "mixed"
    checks = [
        {"name": "test_command", "level": 1, "blocking": True, "status": "pending", "planned": bool(test_command)},
        {"name": "semantic_ast", "level": 2, "blocking": semantic_strict, "status": "pending" if semantic_enabled else "skipped", "planned": semantic_enabled},
        {"name": "optional_python_tool", "level": 3, "blocking": profile == "strict", "status": "pending" if semantic_enabled else "skipped", "planned": semantic_enabled},
        {"name": "smoke_imports", "level": 4, "blocking": smoke_imports_strict, "status": "pending" if smoke_imports else "skipped", "planned": smoke_imports},
    ]
    return ValidationPlan(
        profile=profile,
        source=source,
        suggested_profile=context.suggested_profile,
        suggestion_reason=context.suggestion_reason,
        promotion_decision=promotion_decision,
        promotion_reasons=context.promotion_reasons,
        promotion_blockers=context.promotion_blockers,
        test_command=test_command,
        lint_command=explicit_lint_cmd,
        semantic_validation=semantic_enabled,
        semantic_validation_mode=semantic_mode,
        semantic_validation_strict=semantic_strict,
        semantic_validation_timeout=semantic_timeout,
        smoke_imports=smoke_imports,
        smoke_imports_strict=smoke_imports_strict,
        smoke_import_runner=context.python_runner if smoke_imports else None,
        validation_evidence_level=validation_evidence_level,
        checks=checks,
        context=context,
    )


def validation_plan_to_dict(plan: ValidationPlan) -> dict[str, Any]:
    return {
        "profile": plan.profile,
        "source": plan.source,
        "suggested_profile": plan.suggested_profile,
        "suggestion_reason": plan.suggestion_reason,
        "promotion_decision": plan.promotion_decision,
        "promotion_reasons": plan.promotion_reasons,
        "promotion_blockers": plan.promotion_blockers,
        "runner": {
            "python": plan.context.python_runner,
            "source": plan.context.python_runner_source,
            "reproducible": plan.context.runner_reproducible,
            "usable": plan.context.runner_usable,
        },
        "test_command": plan.test_command,
        "lint_command": plan.lint_command,
        "semantic_validation": {
            "enabled": plan.semantic_validation,
            "mode": plan.semantic_validation_mode,
            "strict": plan.semantic_validation_strict,
            "timeout_seconds": plan.semantic_validation_timeout,
        },
        "smoke_imports": {
            "enabled": plan.smoke_imports,
            "strict": plan.smoke_imports_strict,
            "runner": plan.smoke_import_runner,
        },
        "validation_evidence_level": plan.validation_evidence_level,
        "checks": plan.checks,
        "context": dataclasses.asdict(plan.context),
    }


def infer_validation_changed_files(
    repo: Path,
    args,
    *,
    infer_base_ref_fn=infer_base_ref,
    collect_git_changed_files_fn=collect_git_changed_files,
    run_cmd_fn=run_cmd,
) -> tuple[list[str], str]:
    base_ref = (getattr(args, "base_ref", "") or "").strip()
    if not base_ref:
        try:
            base_ref = infer_base_ref_fn(repo)
        except Exception:
            base_ref = "HEAD~1"
    changed = collect_git_changed_files_fn(repo, base_ref)
    if changed:
        return changed, base_ref
    code, out, _ = run_cmd_fn(["git", "diff", "--name-only", "HEAD"], cwd=repo, capture=True)
    if code == 0:
        fallback = [line.strip() for line in out.splitlines() if line.strip()]
        if fallback:
            return fallback, "HEAD"
    return [], base_ref


def write_test_report(
    repo: Path,
    ai_dir: Path,
    lint_cmd: Optional[str],
    test_cmd: str,
    *,
    lint_required: bool = False,
    timeout: int = 1800,
    changed_files: Optional[list[str]] = None,
    semantic_validation: bool = False,
    semantic_validation_mode: str = "auto",
    semantic_validation_strict: bool = False,
    semantic_validation_timeout: int = 60,
    validation_plan: Optional[ValidationPlan] = None,
    audit_python_dependency_declarations=None,
    run_cmd_fn=run_cmd,
    detect_validation_context_fn=detect_validation_context,
    run_semantic_validation_fn=run_semantic_validation,
    run_python_smoke_imports_fn=run_python_smoke_imports,
) -> tuple[bool, str, TestReportSummary]:
    entries = []
    summary: TestReportSummary = {
        "validation_profile": None,
        "lint": None,
        "tests": None,
        "lint_required": lint_required,
        "passed": False,
        "timestamp": now_stamp(),
        "semantic_validation": {
            "enabled": semantic_validation,
            "status": "skipped",
            "issues": [],
            "warnings": [],
            "files_checked": [],
            "tools": [],
        },
        "smoke_imports": {
            "status": "skipped",
            "issues": [],
            "warnings": [],
            "modules": [],
        },
        "checks": [],
        "status": "failed",
        "validation_evidence_level": "mixed",
    }
    if validation_plan is None:
        validation_context = detect_validation_context_fn(repo)
        validation_plan = ValidationPlan(
            profile="manual",
            source="manual",
            suggested_profile=validation_context.suggested_profile,
            suggestion_reason=validation_context.suggestion_reason,
            promotion_decision="manual",
            promotion_reasons=validation_context.promotion_reasons,
            promotion_blockers=validation_context.promotion_blockers,
            test_command=test_cmd,
            lint_command=lint_cmd,
            semantic_validation=semantic_validation,
            semantic_validation_mode=semantic_validation_mode,
            semantic_validation_strict=semantic_validation_strict,
            semantic_validation_timeout=semantic_validation_timeout,
            smoke_imports=False,
            smoke_imports_strict=False,
            smoke_import_runner=None,
            validation_evidence_level="mixed",
            checks=[],
            context=validation_context,
        )
    safe_write(ai_dir / "validation-execution-plan.json", json.dumps(validation_plan_to_dict(validation_plan), indent=2, ensure_ascii=False))
    lint_cmd = validation_plan.lint_command
    test_cmd = validation_plan.test_command
    summary["validation_profile"] = validation_plan_to_dict(validation_plan)
    summary["validation_evidence_level"] = validation_plan.validation_evidence_level

    def run_and_capture(label: str, cmd: str) -> CommandExecutionResult:
        use_shell = looks_like_shell_command(cmd)
        try:
            rc, out, err = run_cmd_fn(cmd, cwd=repo, capture=True, use_shell=use_shell, timeout=timeout)
            status = "ok" if rc == 0 else "failed"
            return {"label": label, "cmd": cmd, "returncode": rc, "stdout": out, "stderr": err, "use_shell": use_shell, "status": status, "timeout_seconds": None}
        except subprocess.TimeoutExpired as exc:
            return {"label": label, "cmd": cmd, "returncode": None, "stdout": to_text(exc.stdout), "stderr": to_text(exc.stderr), "use_shell": use_shell, "status": "timeout", "timeout_seconds": timeout}

    lint_ok = True
    if lint_cmd:
        lint_res = run_and_capture("lint", lint_cmd)
        lint_ok = lint_res["returncode"] == 0
        summary["lint"] = {"returncode": lint_res["returncode"], "status": lint_res["status"], "timeout_seconds": lint_res["timeout_seconds"]}
        entries.append(lint_res)
        summary["checks"].append(build_validation_check("lint", 1, lint_res["status"], lint_required))

    test_res = run_and_capture("tests", test_cmd)
    test_ok = test_res["returncode"] == 0
    summary["tests"] = {"returncode": test_res["returncode"], "status": test_res["status"], "timeout_seconds": test_res["timeout_seconds"]}
    entries.append(test_res)
    summary["checks"].append(build_validation_check("test_command", 1, test_res["status"], True, command=test_cmd))

    passed = test_ok and (lint_ok or not lint_required)
    summary["passed"] = passed
    semantic_summary = run_semantic_validation_fn(
        repo,
        changed_files or [],
        enabled=validation_plan.semantic_validation,
        mode=validation_plan.semantic_validation_mode,
        strict=validation_plan.semantic_validation_strict,
        timeout=validation_plan.semantic_validation_timeout,
        audit_python_dependency_declarations=audit_python_dependency_declarations,
    )
    summary["semantic_validation"] = semantic_summary
    summary["checks"].append(build_validation_check("semantic_ast", 2, semantic_summary["status"], validation_plan.semantic_validation_strict))
    tool_status = semantic_summary["tools"][0]["status"] if semantic_summary["tools"] else "skipped"
    summary["checks"].append(build_validation_check("optional_python_tool", 3, tool_status, validation_plan.profile == "strict"))
    smoke_summary = run_python_smoke_imports_fn(repo, changed_files or [], validation_plan.smoke_import_runner, validation_plan.semantic_validation_timeout) if validation_plan.smoke_imports else {"status": "skipped", "issues": [], "warnings": ["Smoke imports omitted."], "modules": []}
    summary["smoke_imports"] = smoke_summary
    summary["checks"].append(build_validation_check("smoke_imports", 4, smoke_summary["status"], validation_plan.smoke_imports_strict))
    if any(e["status"] == "timeout" for e in entries):
        summary["status"] = "timeout"
    elif semantic_summary["status"] == "failed":
        summary["status"] = "failed"
        summary["passed"] = False
    elif smoke_summary["status"] == "failed":
        summary["status"] = "failed" if validation_plan.smoke_imports_strict else "warning"
        if validation_plan.smoke_imports_strict:
            summary["passed"] = False
    elif semantic_summary["status"] == "warning":
        summary["status"] = "warning" if passed else "failed"
    elif smoke_summary["status"] == "warning":
        summary["status"] = "warning" if passed else "failed"
    else:
        summary["status"] = "ok" if passed else "failed"
    passed = summary["passed"]

    md_parts = [f"# Test Report\n\nTimestamp: {summary['timestamp']}\n"]
    md_parts.append(f"- lint_required: **{lint_required}**\n")
    md_parts.append(f"- overall_passed: **{summary['passed']}**\n")
    md_parts.append(f"- stage_status: **{summary['status']}**\n")
    md_parts.append(f"- semantic_validation_status: **{semantic_summary['status']}**\n")
    md_parts.append(f"- validation_profile: **{validation_plan.profile}** ({validation_plan.source})\n")
    md_parts.append(f"- validation_evidence_level: **{validation_plan.validation_evidence_level}**\n")
    md_parts.append(f"- suggested_profile: **{validation_plan.suggested_profile}**\n")
    md_parts.append(f"- profile_reason: {validation_plan.suggestion_reason}\n")
    md_parts.append(f"- promotion_decision: **{validation_plan.promotion_decision}**\n")
    md_parts.append(f"- runner: **{validation_plan.context.python_runner or 'none'}** ({validation_plan.context.python_runner_source})\n")

    for e in entries:
        md_parts.append(f"\n## {e['label'].title()}\n")
        md_parts.append(f"Command:\n```bash\n{e['cmd']}\n```\n")
        md_parts.append(f"Used shell: **{e['use_shell']}**\n")
        md_parts.append(f"Status: **{e['status']}**\n")
        md_parts.append(f"Return code: **{e['returncode']}**\n")
        if e["timeout_seconds"]:
            md_parts.append(f"Timeout seconds: **{e['timeout_seconds']}**\n")
        md_parts.append("STDOUT:\n```text\n" + truncate(e["stdout"], 15000) + "\n```\n")
        md_parts.append("STDERR:\n```text\n" + truncate(e["stderr"], 15000) + "\n```\n")

    md_parts.append("\n## Semantic Validation\n")
    md_parts.append(f"- enabled: **{semantic_summary['enabled']}**\n")
    md_parts.append(f"- status: **{semantic_summary['status']}**\n")
    if semantic_summary["files_checked"]:
        md_parts.append("- files_checked:\n")
        md_parts.extend(f"  - `{path}`\n" for path in semantic_summary["files_checked"])
    if semantic_summary["issues"]:
        md_parts.append("- issues:\n")
        for issue in semantic_summary["issues"]:
            location = f" ({issue['file']}:{issue.get('line')})" if issue.get("file") else ""
            md_parts.append(f"  - [{issue['kind']}] {issue['message']}{location}\n")
    if semantic_summary["warnings"]:
        md_parts.append("- warnings:\n")
        for warning in semantic_summary["warnings"]:
            md_parts.append(f"  - {warning}\n")
    if semantic_summary["tools"]:
        md_parts.append("- tools:\n")
        for tool in semantic_summary["tools"]:
            name = tool.get("tool") or "none"
            md_parts.append(f"  - `{name}`: {tool.get('status', 'unknown')}\n")

    md_parts.append("\n## Smoke Imports\n")
    md_parts.append(f"- status: **{smoke_summary['status']}**\n")
    if smoke_summary.get("modules"):
        md_parts.append("- modules:\n")
        for module in smoke_summary["modules"]:
            md_parts.append(f"  - `{module}`\n")
    if smoke_summary.get("issues"):
        md_parts.append("- issues:\n")
        for issue in smoke_summary["issues"]:
            md_parts.append(f"  - [{issue['kind']}] {issue['module']}: {issue['message']}\n")
    if smoke_summary.get("warnings"):
        md_parts.append("- warnings:\n")
        for warning in smoke_summary["warnings"]:
            md_parts.append(f"  - {warning}\n")

    md_parts.append("\n## Validation Checks\n")
    for check in summary["checks"]:
        md_parts.append(f"- L{check['level']} `{check['name']}`: **{check['status']}** blocking={check['blocking']}\n")

    md_parts.append("\n## Validation Plan\n")
    if validation_plan.promotion_reasons:
        md_parts.append("- promotion_reasons:\n")
        for reason in validation_plan.promotion_reasons:
            md_parts.append(f"  - {reason}\n")
    if validation_plan.promotion_blockers:
        md_parts.append("- promotion_blockers:\n")
        for blocker in validation_plan.promotion_blockers:
            md_parts.append(f"  - {blocker}\n")
    if validation_plan.context.repo_notes:
        md_parts.append("- repo_notes:\n")
        for note in validation_plan.context.repo_notes:
            md_parts.append(f"  - {note}\n")

    report_md = "\n".join(md_parts)
    safe_write(ai_dir / "test-report.md", report_md)
    safe_write(ai_dir / "test-report.json", json.dumps(summary, indent=2, ensure_ascii=False))
    return passed, report_md, summary
