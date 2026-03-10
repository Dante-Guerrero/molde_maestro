from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


def _normalize_cfg_keys(cfg: Any) -> Any:
    if isinstance(cfg, dict):
        out: dict[str, Any] = {}
        for k, v in cfg.items():
            nk = str(k).strip().replace("-", "_")
            out[nk] = _normalize_cfg_keys(v)
        return out
    if isinstance(cfg, list):
        return [_normalize_cfg_keys(x) for x in cfg]
    return cfg


def load_config_file(config_path: str | None) -> tuple[dict, Optional[Path]]:
    cwd = Path.cwd()
    candidates: list[Path] = []

    if config_path:
        cp = Path(config_path).expanduser()
        if not cp.is_absolute():
            cp = cwd / cp
        candidates.append(cp)
    else:
        candidates += [
            cwd / "molde_maestro.yml",
            cwd / "molde_maestro.yaml",
            cwd / "molde_maestro.json",
        ]

    chosen = next((p for p in candidates if p.exists()), None)
    if not chosen:
        return {}, None

    suffix = chosen.suffix.lower()

    if suffix in (".yml", ".yaml"):
        try:
            import yaml  # type: ignore
        except ImportError:
            if config_path:
                raise SystemExit("Hay config YAML pero falta PyYAML. Instala: pip install pyyaml")
            print(
                f"warning: se ignoró {chosen} porque falta PyYAML; continúa con flags/defaults.",
                file=sys.stderr,
            )
            return {}, None
        raw = yaml.safe_load(chosen.read_text(encoding="utf-8")) or {}
    elif suffix == ".json":
        raw = json.loads(chosen.read_text(encoding="utf-8"))
    else:
        return {}, chosen

    cfg = _normalize_cfg_keys(raw)
    if not isinstance(cfg, dict):
        raise SystemExit("El config debe ser un objeto/dict en YAML o JSON.")
    return cfg, chosen


def merge_config_into_args(args: argparse.Namespace, cfg: dict) -> argparse.Namespace:
    def has(field: str) -> bool:
        return hasattr(args, field)

    def get(field: str, default=None):
        return getattr(args, field, default)

    def set_(field: str, value):
        setattr(args, field, value)

    mappings = {
        "repo": "repo",
        "goals": "goals",
        "goals_text": "goals_text",
        "ai_dir": "ai_dir",
        "extra_context": "extra_context",
        "max_tree_lines": "max_tree_lines",
        "reasoner": "reasoner",
        "aider_model": "aider_model",
        "test_cmd": "test_cmd",
        "lint_cmd": "lint_cmd",
        "max_iters": "max_iters",
        "zip": "zip",
        "branch": "branch",
        "base": "base",
        "base_branch": "base",
        "allowed_file": "allowed_file",
        "allowed_files": "allowed_file",
        "aider_extra_arg": "aider_extra_arg",
        "aider_extra_args": "aider_extra_arg",
        "lint_required": "lint_required",
        "allow_dirty_repo": "allow_dirty_repo",
        "reasoner_timeout": "reasoner_timeout",
        "report_timeout": "report_timeout",
        "aider_timeout": "aider_timeout",
        "test_timeout": "test_timeout",
        "plan_mode": "plan_mode",
        "plan_max_tree_lines": "plan_max_tree_lines",
        "plan_max_files": "plan_max_files",
        "plan_max_file_chars": "plan_max_file_chars",
        "plan_max_changes": "plan_max_changes",
        "plan_fallback_reasoner": "plan_fallback_reasoner",
        "plan_fallback_timeout": "plan_fallback_timeout",
        "plan_retry_on_timeout": "plan_retry_on_timeout",
        "apply_max_plan_changes": "apply_max_plan_changes",
        "apply_limit_to_plan_files": "apply_limit_to_plan_files",
        "apply_enforce_plan_scope": "apply_enforce_plan_scope",
        "apply_skip_unsupported_plan_changes": "apply_skip_unsupported_plan_changes",
        "semantic_validation": "semantic_validation",
        "validation_profile": "validation_profile",
        "semantic_validation_mode": "semantic_validation_mode",
        "semantic_validation_strict": "semantic_validation_strict",
        "semantic_validation_timeout": "semantic_validation_timeout",
    }

    for ck, af in mappings.items():
        if ck not in cfg or not has(af):
            continue

        val = cfg[ck]
        cur = get(af)

        if isinstance(cur, str):
            default_strings = {
                "repo": ".",
                "goals": "PROJECT_GOALS.md",
                "goals_text": "",
                "ai_dir": "AI",
                "plan_mode": "balanced",
                "plan_fallback_reasoner": "",
                "validation_profile": "auto",
                "semantic_validation_mode": "auto",
            }
            if not str(cur).strip() or (af in default_strings and cur == default_strings[af]):
                set_(af, str(val))
        elif isinstance(cur, list):
            if not cur and isinstance(val, list):
                set_(af, val)
        elif isinstance(cur, bool):
            if cur is False and bool(val) is True:
                set_(af, True)
        elif isinstance(cur, int):
            defaults = {
                "max_iters": 2,
                "max_tree_lines": 600,
                "reasoner_timeout": 1800,
                "report_timeout": 0,
                "aider_timeout": 7200,
                "test_timeout": 1800,
                "plan_max_tree_lines": 0,
                "plan_max_files": 0,
                "plan_max_file_chars": 0,
                "plan_max_changes": 0,
                "plan_fallback_timeout": 0,
                "apply_max_plan_changes": 0,
                "semantic_validation_timeout": 60,
            }
            if af in defaults and cur == defaults[af]:
                set_(af, int(val))
            elif cur in (0, None):
                set_(af, int(val))
        else:
            if cur is None:
                set_(af, val)

    return args


def resolve_config_relative_paths(args: argparse.Namespace, cfg: dict, cfg_path: Optional[Path]) -> argparse.Namespace:
    if not cfg_path:
        return args

    cfg_dir = cfg_path.parent

    if "repo" in cfg and hasattr(args, "repo"):
        repo_value = str(getattr(args, "repo", "") or "").strip()
        if repo_value:
            repo_path = Path(repo_value).expanduser()
            if not repo_path.is_absolute():
                args.repo = str((cfg_dir / repo_path).resolve())

    return args


def _is_interactive() -> bool:
    return sys.stdin.isatty()


def _list_ollama_models() -> list[str]:
    try:
        proc = subprocess.run(
            ["ollama", "list"],
            text=True,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    models: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            models.append(parts[0])
    return models


def _prompt_numbered_model_selection(label: str, models: list[str]) -> str:
    if not models:
        raise SystemExit(f"No pude listar modelos de Ollama para seleccionar {label}.")
    print(f"Selecciona el modelo {label}:")
    for index, model in enumerate(models, start=1):
        print(f"{index}. {model}")
    while True:
        choice = input("> ").strip()
        if choice.isdigit():
            selected = int(choice)
            if 1 <= selected <= len(models):
                return f"ollama:{models[selected - 1]}"
        print(f"Entrada inválida. Ingresa un número entre 1 y {len(models)}.", file=sys.stderr)


def _prompt_multiline_goals() -> str:
    print("No se encontró el archivo de goals. Pega las instrucciones y finaliza con una línea que contenga solo END.")
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    if not text:
        raise SystemExit("No se ingresaron objetivos para el repositorio.")
    return text


def complete_interactive_args(args: argparse.Namespace) -> argparse.Namespace:
    if not _is_interactive():
        return args

    needs_reasoner = args.cmd in {"plan", "run", "report"} and not str(getattr(args, "reasoner", "") or "").strip()
    needs_aider = args.cmd in {"apply", "run"} and not str(getattr(args, "aider_model", "") or "").strip()
    needs_goals = args.cmd in {"plan", "report", "run"} and not str(getattr(args, "goals_text", "") or "").strip()

    models: list[str] = []
    if needs_reasoner or needs_aider:
        models = _list_ollama_models()

    if needs_reasoner:
        args.reasoner = _prompt_numbered_model_selection("reasoner", models)

    if needs_aider:
        args.aider_model = _prompt_numbered_model_selection("para Aider", models)

    if needs_goals:
        repo = Path(getattr(args, "repo", ".")).expanduser().resolve()
        goals_value = Path(str(getattr(args, "goals", "PROJECT_GOALS.md") or "PROJECT_GOALS.md")).expanduser()
        goals_path = goals_value if goals_value.is_absolute() else repo / goals_value
        if not goals_path.exists():
            args.goals_text = _prompt_multiline_goals()
            args._interactive_goals_text = True

    return args


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Repo improvement pipeline (reasoner -> aider -> tests -> final report).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--config", default="", help="Ruta al config YAML/JSON.")
    sub = p.add_subparsers(dest="cmd")

    def add_shared(sp):
        sp.add_argument("--repo", default=".", help="Ruta del repo objetivo.")
        sp.add_argument("--goals", default="PROJECT_GOALS.md", help="Archivo de objetivos.")
        sp.add_argument("--goals-text", default="", help="Texto inline con objetivos. Tiene prioridad sobre --goals.")
        sp.add_argument("--ai-dir", default="AI", help="Directorio de artefactos AI.")
        sp.add_argument("--reasoner", default="", help="Modelo reasoner. Ej: ollama:deepseek-r1")
        sp.add_argument("--plan-mode", default="balanced", help="Estrategia de planning: fast, balanced o deep.")
        sp.add_argument("--plan-max-tree-lines", type=int, default=0, help="Override del máximo de líneas del árbol enviadas al reasoner.")
        sp.add_argument("--plan-max-files", type=int, default=0, help="Override del máximo de archivos incluidos en el contexto de planning.")
        sp.add_argument("--plan-max-file-chars", type=int, default=0, help="Override del máximo de caracteres por archivo en planning.")
        sp.add_argument("--plan-max-changes", type=int, default=0, help="Override del máximo de cambios que debe proponer el plan.")
        sp.add_argument("--plan-fallback-reasoner", default="", help="Modelo alternativo para planning si el principal entra en timeout.")
        sp.add_argument("--plan-fallback-timeout", type=int, default=0, help="Timeout del fallback de planning. Si es 0, usa reasoner-timeout.")
        sp.add_argument("--plan-retry-on-timeout", action="store_true", help="Si se activa, reintenta planning con modo fast y/o fallback al hacer timeout.")
        sp.add_argument("--apply-max-plan-changes", type=int, default=0, help="Máximo de cambios del plan a implementar. Si es 0, usa modo automático por plan-mode.")
        sp.add_argument("--apply-limit-to-plan-files", action="store_true", help="Limita Aider a los archivos explícitamente listados en el plan o en --allowed-file.")
        sp.add_argument("--apply-enforce-plan-scope", action="store_true", help="Falla si Aider modifica archivos fuera del scope seleccionado.")
        sp.add_argument("--apply-skip-unsupported-plan-changes", action="store_true", help="Omite cambios del plan que parecen requerir archivos o dependencias no presentes en el repo.")
        sp.add_argument("--validation-profile", default="auto", help="Perfil de validación: auto, minimal, python_local, python_repo o strict.")
        sp.add_argument("--semantic-validation", action="store_true", help="Activa validación semántica localizada sobre archivos Python cambiados.")
        sp.add_argument("--semantic-validation-mode", default="auto", help="Modo de validación semántica: auto, ast, ruff o pyflakes.")
        sp.add_argument("--semantic-validation-strict", action="store_true", help="Si se activa, los hallazgos semánticos fallan la corrida.")
        sp.add_argument("--semantic-validation-timeout", type=int, default=60, help="Timeout para herramientas opcionales de validación semántica.")
        sp.add_argument("--aider-model", default="", help="Modelo para Aider. Ej: ollama:qwen3-coder:14b")
        sp.add_argument("--reasoner-timeout", type=int, default=1800, help="Timeout en segundos para el reasoner del plan.")
        sp.add_argument("--report-timeout", type=int, default=0, help="Timeout en segundos para el reporte final. Si es 0, usa reasoner-timeout.")
        sp.add_argument("--aider-timeout", type=int, default=7200, help="Timeout en segundos para Aider.")
        sp.add_argument("--test-timeout", type=int, default=1800, help="Timeout en segundos para lint/tests.")
        sp.add_argument("--test-cmd", default="", help="Comando de tests.")
        sp.add_argument("--lint-cmd", default="", help="Comando de lint.")
        sp.add_argument("--lint-required", action="store_true", help="Si se activa, lint también bloquea el pipeline.")
        sp.add_argument("--allow-dirty-repo", action="store_true", help="Permite correr sobre un repo con cambios sin commit.")
        sp.add_argument("--max-iters", type=int, default=2, help="Máximo de iteraciones Aider->tests.")
        sp.add_argument("--zip", action="store_true", help="Crear snapshot zip.")
        sp.add_argument("--branch", default="", help="Nombre de la rama a crear.")
        sp.add_argument("--base", default="", help="Rama/ref base para apply/run.")
        sp.add_argument("--base-ref", default="", help="Base ref para report/diff.")
        sp.add_argument("--allowed-file", action="append", default=[], help="Archivo permitido para Aider (repetible).")
        sp.add_argument("--aider-extra-arg", action="append", default=[], help="Argumento extra para Aider (repetible).")
        sp.add_argument("--extra-context", action="append", default=[], help="Archivo adicional de contexto para el plan.")
        sp.add_argument("--max-tree-lines", type=int, default=600, help="Máximo de líneas del árbol del repo.")

    sp_snapshot = sub.add_parser("snapshot", help="Crear snapshot zip del repo.")
    add_shared(sp_snapshot)
    sp_snapshot.add_argument("--ref", default="HEAD", help="Git ref a archivar.")
    sp_snapshot.add_argument("--out", default="", help="Ruta de salida para el zip.")

    sp_plan = sub.add_parser("plan", help="Generar AI/plan.md con el reasoner.")
    add_shared(sp_plan)
    sp_plan.add_argument("--plan-out", default="", help="Ruta de salida del plan.")

    sp_apply = sub.add_parser("apply", help="Aplicar AI/plan.md con Aider.")
    add_shared(sp_apply)

    sp_test = sub.add_parser("test", help="Ejecutar lint/tests y generar test-report.")
    add_shared(sp_test)

    sp_report = sub.add_parser("report", help="Generar AI/final.md.")
    add_shared(sp_report)

    sp_run = sub.add_parser("run", help="Ejecutar pipeline completo.")
    add_shared(sp_run)

    return p


def dispatch_command(args) -> None:
    from .commands import cmd_apply, cmd_plan, cmd_report, cmd_run, cmd_snapshot, cmd_test

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
        raise SystemExit(f"Unknown command: {args.cmd}")


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cfg, cfg_path = load_config_file(getattr(args, "config", "") or None)

    if not getattr(args, "cmd", None):
        if cfg:
            args.cmd = "run"
        else:
            parser.print_help()
            raise SystemExit(1)

    if cfg:
        args = merge_config_into_args(args, cfg)
        args = resolve_config_relative_paths(args, cfg, cfg_path)
    args._config_path = cfg_path
    args._interactive_goals_text = False
    args = complete_interactive_args(args)

    try:
        dispatch_command(args)
    except subprocess.TimeoutExpired:
        raise SystemExit("Timed out running a command (model/aider/tests).")
    except RuntimeError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
