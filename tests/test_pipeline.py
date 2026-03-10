import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from molde_maestro import pipeline


class NormalizeModelMarkdownTests(unittest.TestCase):
    def test_strips_reasoning_preamble_before_heading(self) -> None:
        raw = "Thinking...\nStill thinking...\n# Repo Review Plan\n\n## Summary\n- ok\n"
        result = pipeline.normalize_model_markdown(raw, "# Repo Review Plan")
        self.assertEqual(result, "# Repo Review Plan\n\n## Summary\n- ok")

    def test_strips_think_block(self) -> None:
        raw = "<think>internal reasoning</think>\n# Final Report\n\nAll good.\n"
        result = pipeline.normalize_model_markdown(raw, "# Final Report")
        self.assertEqual(result, "# Final Report\n\nAll good.")

    def test_adds_fallback_title_when_heading_is_missing(self) -> None:
        result = pipeline.normalize_model_markdown("plain body", "# Repo Review Plan")
        self.assertEqual(result, "# Repo Review Plan\n\nplain body")

    def test_sanitize_plan_commands_replaces_with_allowed_list(self) -> None:
        plan_md = "# Repo Review Plan\n\n## Commands to validate\n- fake cmd\n- another fake\n"
        result = pipeline.sanitize_plan_commands(plan_md, ["python3 -m compileall src main.py"])
        self.assertIn("`python3 -m compileall src main.py`", result)
        self.assertNotIn("fake cmd", result)

    def test_normalize_aider_model_name_converts_ollama_prefix(self) -> None:
        self.assertEqual(
            pipeline.normalize_aider_model_name("ollama:qwen2.5-coder:14b"),
            "ollama/qwen2.5-coder:14b",
        )


class LoadConfigFileTests(unittest.TestCase):
    def test_ignores_auto_yaml_when_pyyaml_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = Path(tmpdir)
            (cwd / "molde_maestro.yml").write_text("repo: ../revisor\n", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(cwd)
                cfg, chosen = pipeline.load_config_file(None)
            finally:
                os.chdir(old_cwd)
        self.assertEqual(cfg, {})
        self.assertIsNone(chosen)


class PreflightTests(unittest.TestCase):
    def make_args(self, cmd: str) -> argparse.Namespace:
        return argparse.Namespace(
            cmd=cmd,
            reasoner="ollama:deepseek-r1",
            plan_mode="balanced",
            plan_max_tree_lines=0,
            plan_max_files=0,
            plan_max_file_chars=0,
            plan_max_changes=0,
            plan_fallback_reasoner="",
            plan_fallback_timeout=0,
            plan_retry_on_timeout=False,
            aider_model="ollama:qwen2.5-coder:14b",
            goals="PROJECT_GOALS.md",
            test_cmd="python3 -m compileall src",
            lint_cmd="",
            allow_dirty_repo=False,
        )

    @mock.patch("molde_maestro.pipeline.git_status_porcelain", return_value="")
    @mock.patch("molde_maestro.pipeline.command_exists", return_value=True)
    @mock.patch("molde_maestro.pipeline.ensure_git_repo")
    @mock.patch("molde_maestro.pipeline.validate_model_available")
    def test_plan_only_validates_reasoner(
        self,
        mock_validate: mock.Mock,
        _mock_repo: mock.Mock,
        _mock_exists: mock.Mock,
        _mock_status: mock.Mock,
    ) -> None:
        repo = Path("/tmp/repo")
        args = self.make_args("plan")
        with mock.patch("pathlib.Path.exists", return_value=True):
            pipeline.preflight(args, repo)
        self.assertEqual(mock_validate.call_count, 1)
        self.assertEqual(mock_validate.call_args[0][0].name, "deepseek-r1")

    @mock.patch("molde_maestro.pipeline.git_status_porcelain", return_value="")
    @mock.patch("molde_maestro.pipeline.command_exists", return_value=True)
    @mock.patch("molde_maestro.pipeline.ensure_git_repo")
    @mock.patch("molde_maestro.pipeline.validate_model_available")
    def test_apply_only_validates_aider_model(
        self,
        mock_validate: mock.Mock,
        _mock_repo: mock.Mock,
        _mock_exists: mock.Mock,
        _mock_status: mock.Mock,
    ) -> None:
        repo = Path("/tmp/repo")
        args = self.make_args("apply")
        pipeline.preflight(args, repo)
        self.assertEqual(mock_validate.call_count, 1)
        self.assertEqual(mock_validate.call_args[0][0].name, "qwen2.5-coder:14b")


class MergeConfigTests(unittest.TestCase):
    def test_merges_timeout_values_from_config(self) -> None:
        parser = pipeline.build_parser()
        args = parser.parse_args(["run"])
        cfg = {
            "reasoner_timeout": 11,
            "report_timeout": 22,
            "aider_timeout": 33,
            "test_timeout": 44,
            "plan_mode": "fast",
            "plan_max_files": 5,
            "plan_fallback_reasoner": "ollama:qwen2.5-coder:7b",
            "plan_fallback_timeout": 55,
            "apply_max_plan_changes": 1,
            "apply_limit_to_plan_files": True,
            "apply_enforce_plan_scope": True,
            "apply_skip_unsupported_plan_changes": True,
            "validation_profile": "python_local",
            "semantic_validation": True,
            "semantic_validation_mode": "ast",
            "semantic_validation_strict": True,
            "semantic_validation_timeout": 12,
        }
        merged = pipeline.merge_config_into_args(args, cfg)
        self.assertEqual(merged.reasoner_timeout, 11)
        self.assertEqual(merged.report_timeout, 22)
        self.assertEqual(merged.aider_timeout, 33)
        self.assertEqual(merged.test_timeout, 44)
        self.assertEqual(merged.plan_mode, "fast")
        self.assertEqual(merged.plan_max_files, 5)
        self.assertEqual(merged.plan_fallback_reasoner, "ollama:qwen2.5-coder:7b")
        self.assertEqual(merged.plan_fallback_timeout, 55)
        self.assertEqual(merged.apply_max_plan_changes, 1)
        self.assertTrue(merged.apply_limit_to_plan_files)
        self.assertTrue(merged.apply_enforce_plan_scope)
        self.assertTrue(merged.apply_skip_unsupported_plan_changes)
        self.assertEqual(merged.validation_profile, "python_local")
        self.assertTrue(merged.semantic_validation)
        self.assertEqual(merged.semantic_validation_mode, "ast")
        self.assertTrue(merged.semantic_validation_strict)
        self.assertEqual(merged.semantic_validation_timeout, 12)


class PlanStrategyTests(unittest.TestCase):
    def make_args(self, **overrides) -> argparse.Namespace:
        base = argparse.Namespace(
            plan_mode="balanced",
            plan_max_tree_lines=0,
            plan_max_files=0,
            plan_max_file_chars=0,
            plan_max_changes=0,
            plan_retry_on_timeout=False,
            plan_fallback_reasoner="",
            plan_fallback_timeout=0,
            reasoner="ollama:deepseek-r1",
            reasoner_timeout=30,
            extra_context=[],
            allowed_file=[],
            apply_max_plan_changes=0,
            apply_limit_to_plan_files=True,
            apply_enforce_plan_scope=True,
            apply_skip_unsupported_plan_changes=False,
            validation_profile="auto",
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def test_resolve_plan_settings_uses_preset_defaults(self) -> None:
        settings = pipeline.resolve_plan_settings(self.make_args(plan_mode="fast"))
        self.assertEqual(settings.mode, "fast")
        self.assertEqual(settings.max_files, pipeline.PLAN_MODE_PRESETS["fast"]["max_files"])
        self.assertEqual(settings.max_changes, pipeline.PLAN_MODE_PRESETS["fast"]["max_changes"])

    def test_build_plan_attempt_specs_adds_fast_retry_and_fallback(self) -> None:
        attempts = pipeline.build_plan_attempt_specs(
            self.make_args(
                plan_mode="balanced",
                plan_retry_on_timeout=True,
                plan_fallback_reasoner="ollama:qwen2.5-coder:7b",
                plan_fallback_timeout=45,
            )
        )
        self.assertEqual([a.label for a in attempts], ["primary", "retry-fast", "fallback-fast"])
        self.assertEqual(attempts[-1].reasoner, "ollama:qwen2.5-coder:7b")
        self.assertEqual(attempts[-1].timeout, 45)

    def test_build_reasoner_prompt_includes_allowed_validation_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "README.md").write_text("hello", encoding="utf-8")
            settings = pipeline.resolve_plan_settings(self.make_args(plan_mode="fast"))
            prompt = pipeline.build_reasoner_prompt(
                repo,
                "- improve\n",
                [],
                settings,
                ["python3 -m compileall src main.py"],
            )
            self.assertIn("ALLOWED VALIDATION COMMANDS", prompt)
            self.assertIn("python3 -m compileall src main.py", prompt)


class ApplyScopeTests(unittest.TestCase):
    def make_args(self, **overrides) -> argparse.Namespace:
        base = argparse.Namespace(
            plan_mode="fast",
            allowed_file=[],
            apply_max_plan_changes=0,
            apply_limit_to_plan_files=True,
            apply_enforce_plan_scope=True,
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def test_parse_plan_changes_extracts_titles_and_files(self) -> None:
        plan_md = """# Repo Review Plan

## Changes (ordered)
1) Change: Replace placeholder page function with real rendering.
   - Files: `src/exam_pipeline/ingest.py`
   - Rationale: ...
2) Change: Enhance JSON schema validation.
   - Files: src/exam_pipeline/grade.py, src/exam_pipeline/transcribe.py
   - Rationale: ...
"""
        changes = pipeline.parse_plan_changes(plan_md)
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[0].files, ["src/exam_pipeline/ingest.py"])
        self.assertEqual(
            changes[1].files,
            ["src/exam_pipeline/grade.py", "src/exam_pipeline/transcribe.py"],
        )

    def test_resolve_apply_scope_limits_fast_mode_to_first_change_files(self) -> None:
        plan_md = """# Repo Review Plan

## Changes (ordered)
1) Change: Replace placeholder page function with real rendering.
   - Files: `src/exam_pipeline/ingest.py`
   - Rationale: ...
2) Change: Enhance JSON schema validation.
   - Files: src/exam_pipeline/grade.py, src/exam_pipeline/transcribe.py
   - Rationale: ...
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "src/exam_pipeline").mkdir(parents=True)
            (repo / "src/exam_pipeline/ingest.py").write_text("", encoding="utf-8")
            (repo / "src/exam_pipeline/grade.py").write_text("", encoding="utf-8")
            scope = pipeline.resolve_apply_scope(repo, plan_md, self.make_args())
        self.assertEqual(scope.max_changes, 1)
        self.assertEqual([change.title for change in scope.selected_changes], ["Replace placeholder page function with real rendering."])
        self.assertEqual(scope.selected_files, ["src/exam_pipeline/ingest.py"])

    def test_resolve_apply_scope_skips_changes_with_unsupported_dependencies(self) -> None:
        plan_md = """# Repo Review Plan

## Changes (ordered)
1) Change: Replace placeholder page function with real rendering.
   - Files: `src/exam_pipeline/ingest.py`
   - Rationale: Use `pdf2image` to render pages.
2) Change: Improve preprocessing.
   - Files: `src/exam_pipeline/preprocess.py`
   - Rationale: Tighten the current image processing path.
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "requirements.txt").write_text("pillow\npypdf\n", encoding="utf-8")
            (repo / "src/exam_pipeline").mkdir(parents=True)
            (repo / "src/exam_pipeline/ingest.py").write_text("", encoding="utf-8")
            (repo / "src/exam_pipeline/preprocess.py").write_text("", encoding="utf-8")
            scope = pipeline.resolve_apply_scope(
                repo,
                plan_md,
                self.make_args(apply_skip_unsupported_plan_changes=True),
            )
        self.assertEqual([change.title for change in scope.selected_changes], ["Improve preprocessing."])
        self.assertEqual(scope.selected_files, ["src/exam_pipeline/preprocess.py"])
        self.assertEqual(scope.skipped_change_titles, ["Replace placeholder page function with real rendering."])

    def test_audit_python_dependency_declarations_flags_undeclared_imports(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "requirements.txt").write_text("pillow\npypdf\n", encoding="utf-8")
            (repo / "src/exam_pipeline").mkdir(parents=True)
            (repo / "src/exam_pipeline/ingest.py").write_text(
                "from PIL import Image\nfrom pdf2image import convert_from_path\n",
                encoding="utf-8",
            )
            audit = pipeline.audit_python_dependency_declarations(repo, ["src/exam_pipeline/ingest.py"])
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["issues"][0]["file"], "src/exam_pipeline/ingest.py")
        self.assertIn("pdf2image", audit["issues"][0]["missing_dependencies"])

    def test_audit_python_dependency_declarations_reads_pyproject_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "pyproject.toml").write_text(
                """
[project]
name = "exam-pipeline"
version = "0.1.0"
dependencies = ["requests>=2", "Pillow>=10"]

[project.optional-dependencies]
dev = ["pytest>=8"]
""".strip(),
                encoding="utf-8",
            )
            (repo / "src/exam_pipeline").mkdir(parents=True)
            (repo / "src/exam_pipeline/ingest.py").write_text(
                "import requests\nfrom PIL import Image\n",
                encoding="utf-8",
            )
            audit = pipeline.audit_python_dependency_declarations(repo, ["src/exam_pipeline/ingest.py"])
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["issues"], [])

    def test_default_aider_instruction_includes_scope_and_files(self) -> None:
        instruction = pipeline.default_aider_instruction(
            "# Repo Review Plan",
            selected_plan_md="1) Change: Small fix\n   - Files: `src/foo.py`",
            allowed_files=["src/foo.py"],
            validation_commands=["python3 -m compileall src"],
            plan_mode="fast",
        )
        self.assertIn("Do not edit files outside this allowlist", instruction)
        self.assertIn("src/foo.py", instruction)
        self.assertIn("fast mode", instruction)


class ValidationProfileTests(unittest.TestCase):
    def make_args(self, **overrides) -> argparse.Namespace:
        base = argparse.Namespace(
            test_cmd="",
            lint_cmd="",
            validation_profile="auto",
            semantic_validation=False,
            semantic_validation_mode="auto",
            semantic_validation_strict=False,
            semantic_validation_timeout=60,
        )
        for key, value in overrides.items():
            setattr(base, key, value)
        return base

    def test_detect_validation_context_suggests_python_local_without_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "requirements.txt").write_text("pydantic>=2\n", encoding="utf-8")
            (repo / "src/pkg").mkdir(parents=True)
            context = pipeline.detect_validation_context(repo)
        self.assertEqual(context.suggested_profile, "python_local")
        self.assertIn("without enough reproducible environment evidence", context.suggestion_reason)

    def test_resolve_validation_plan_prefers_pytest_when_repo_supports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "tests").mkdir()
            (repo / "src/pkg").mkdir(parents=True)
            (repo / ".venv/bin").mkdir(parents=True)
            venv_python = repo / ".venv/bin/python"
            venv_python.write_text("", encoding="utf-8")
            with mock.patch("molde_maestro.pipeline.runner_executes_python", return_value=True):
                with mock.patch("molde_maestro.pipeline.python_module_available", return_value=True):
                    plan = pipeline.resolve_validation_plan(repo, self.make_args())
        self.assertEqual(plan.profile, "python_repo")
        self.assertIn("-m pytest -q", plan.test_command)
        self.assertTrue(plan.smoke_imports)
        self.assertEqual(plan.promotion_decision, "promoted")

    def test_resolve_validation_plan_uses_compile_when_pytest_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "requirements.txt").write_text("pydantic>=2\n", encoding="utf-8")
            (repo / "src/pkg").mkdir(parents=True)
            with mock.patch("molde_maestro.pipeline.command_exists", side_effect=lambda name: name == "python3"):
                plan = pipeline.resolve_validation_plan(repo, self.make_args())
        self.assertEqual(plan.profile, "python_local")
        self.assertIn("compileall", plan.test_command)
        self.assertFalse(plan.smoke_imports)
        self.assertIn("No reproducible repo-local Python runner", " ".join(plan.promotion_blockers))

    def test_infer_validation_changed_files_uses_base_ref_diff(self) -> None:
        args = argparse.Namespace(base_ref="")
        with mock.patch("molde_maestro.pipeline.infer_base_ref", return_value="HEAD~1"):
            with mock.patch("molde_maestro.pipeline.collect_git_changed_files", return_value=["src/pkg/mod.py"]):
                changed, base_ref = pipeline.infer_validation_changed_files(Path("/tmp/repo"), args)
        self.assertEqual(base_ref, "HEAD~1")
        self.assertEqual(changed, ["src/pkg/mod.py"])

    def test_validation_plan_to_dict_exposes_runner_and_promotion(self) -> None:
        context = pipeline.ValidationContext(
            is_python_repo=True,
            has_tests=False,
            has_pyproject=False,
            has_requirements=True,
            has_src_dir=True,
            has_main_py=True,
            has_dot_venv=False,
            has_uv_lock=False,
            has_setup_cfg=False,
            python_runner="python3",
            python_runner_source="system",
            runner_reproducible=False,
            runner_usable=True,
            pytest_available=False,
            pytest_command="",
            compile_command="python3 -m compileall src main.py",
            repo_notes=["No repo-local virtual environment detected."],
            suggested_profile="python_local",
            suggestion_reason="reason",
            promotion_reasons=["runner works"],
            promotion_blockers=["no .venv"],
        )
        plan = pipeline.ValidationPlan(
            profile="python_local",
            source="auto",
            suggested_profile="python_local",
            suggestion_reason="reason",
            promotion_decision="not_promoted",
            promotion_reasons=["runner works"],
            promotion_blockers=["no .venv"],
            test_command="python3 -m compileall src main.py",
            lint_command=None,
            semantic_validation=True,
            semantic_validation_mode="ast",
            semantic_validation_strict=True,
            semantic_validation_timeout=60,
            smoke_imports=False,
            smoke_imports_strict=False,
            smoke_import_runner=None,
            checks=[],
            context=context,
        )
        data = pipeline.validation_plan_to_dict(plan)
        self.assertEqual(data["promotion_decision"], "not_promoted")
        self.assertEqual(data["runner"]["python"], "python3")

    def test_python_module_name_for_path_normalizes_src_layout(self) -> None:
        self.assertEqual(pipeline.python_module_name_for_path("src/pkg/mod.py"), "pkg.mod")
        self.assertEqual(pipeline.python_module_name_for_path("src/pkg/__init__.py"), "pkg")
        self.assertEqual(pipeline.python_module_name_for_path("main.py"), "main")

    def test_run_python_smoke_imports_uses_src_root_modules(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "src/pkg").mkdir(parents=True)
            (repo / "src/pkg/mod.py").write_text("VALUE = 1\n", encoding="utf-8")
            smoke = pipeline.run_python_smoke_imports(
                repo,
                ["src/pkg/mod.py", "src/pkg/__init__.py", "main.py"],
                sys.executable,
                timeout=5,
            )
        self.assertEqual(smoke["status"], "failed")
        self.assertIn("pkg.mod", smoke["modules"])
        self.assertIn("pkg", smoke["modules"])
        self.assertIn("main", smoke["modules"])
        self.assertNotIn("src.pkg.mod", smoke["modules"])


class RunRecorderTests(unittest.TestCase):
    def test_persists_stage_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ai_dir = Path(tmpdir)
            recorder = pipeline.RunRecorder(ai_dir=ai_dir, command="run", repo=ai_dir)
            recorder.start_stage("plan")
            recorder.finish_stage("plan", "ok", {"artifact": "plan.md"})
            recorder.complete_run("ok", {"done": True})

            data = json.loads((ai_dir / "run-metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["stages"][0]["name"], "plan")
            self.assertEqual(data["stages"][0]["status"], "ok")
            self.assertEqual(data["stages"][0]["details"]["artifact"], "plan.md")
            self.assertIsNotNone(data["stages"][0]["duration_seconds"])

    def test_init_run_context_uses_report_command_metadata_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            ai_dir = repo / "AI"
            ai_dir.mkdir()
            (ai_dir / "run-metadata.json").write_text("{}", encoding="utf-8")
            args = argparse.Namespace(repo=str(repo), ai_dir="AI", cmd="report", _config_path=None)
            _repo, _ai_dir, recorder = pipeline.init_run_context(args)
            self.assertEqual(recorder.metadata_path.name, "report-command-metadata.json")

    def test_build_fallback_final_report_reflects_failure(self) -> None:
        metadata = {
            "status": "timeout",
            "stages": [
                {"name": "plan", "status": "ok", "duration_seconds": 12.3, "details": {}},
                {"name": "apply", "status": "timeout", "duration_seconds": 50.0, "details": {"changed_files": ["src/foo.py"]}},
            ],
            "error": {"message": "Aider timed out"},
        }
        report = pipeline.build_fallback_final_report(metadata, "1) Change: Fix foo", "")
        self.assertIn("Overall status: **timeout**", report)
        self.assertIn("src/foo.py", report)
        self.assertIn("Aider timed out", report)

    def test_build_grounded_final_report_uses_actual_changed_files(self) -> None:
        metadata = {
            "status": "ok",
            "stages": [
                {"name": "apply", "status": "ok", "duration_seconds": 10.0, "details": {"selected_change_titles": ["Fix ingest"], "changed_files": ["src/exam_pipeline/ingest.py"]}},
                {"name": "test", "status": "ok", "duration_seconds": 1.0, "details": {}},
            ],
        }
        report = pipeline.build_grounded_final_report(
            metadata,
            "1) Change: Fix ingest\n   - Files: `src/exam_pipeline/ingest.py`\n",
            "- stage_status: **ok**",
            ["src/exam_pipeline/ingest.py"],
            "diff --git a/src/exam_pipeline/ingest.py b/src/exam_pipeline/ingest.py",
        )
        self.assertIn("Committed files: src/exam_pipeline/ingest.py", report)
        self.assertNotIn("src/exam_pipeline/preprocess.py", report)
        self.assertIn("grounded on metadata and git diff", report)

    def test_build_grounded_final_report_uses_executed_validation_command(self) -> None:
        metadata = {
            "status": "ok",
            "stages": [
                {"name": "apply", "status": "ok", "duration_seconds": 10.0, "details": {"selected_change_titles": ["Fix ingest"], "changed_files": ["src/exam_pipeline/ingest.py"]}},
                {"name": "test", "status": "ok", "duration_seconds": 1.0, "details": {}},
            ],
        }
        report = pipeline.build_grounded_final_report(
            metadata,
            "1) Change: Fix ingest\n   - Files: `src/exam_pipeline/ingest.py`\n",
            "## Tests\n\nCommand:\n```bash\npython3 -m compileall src\n```\n\n- stage_status: **ok**",
            ["src/exam_pipeline/ingest.py"],
            "diff --git a/src/exam_pipeline/ingest.py b/src/exam_pipeline/ingest.py",
        )
        self.assertIn("`python3 -m compileall src`", report)
        self.assertNotIn("compileall src main.py", report)

    def test_project_reported_metadata_marks_report_ok(self) -> None:
        metadata = {
            "status": "running",
            "stages": [
                {"name": "report", "status": "running", "duration_seconds": None, "details": {}},
            ],
        }
        projected = pipeline.project_reported_metadata(metadata, "ok")
        self.assertEqual(projected["status"], "ok")
        self.assertEqual(projected["stages"][0]["status"], "ok")


class CommandArtifactTests(unittest.TestCase):
    def test_ensure_repo_ready_for_aider_ignores_ai_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / ".git" / "info").mkdir(parents=True)
            pipeline.ensure_repo_ready_for_aider(repo)
            exclude = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn("AI/", exclude)

    def make_plan_args(self, repo: Path) -> argparse.Namespace:
        return argparse.Namespace(
            cmd="plan",
            repo=str(repo),
            ai_dir="AI",
            reasoner="ollama:deepseek-r1",
            reasoner_timeout=12,
            plan_mode="balanced",
            plan_max_tree_lines=0,
            plan_max_files=0,
            plan_max_file_chars=0,
            plan_max_changes=0,
            plan_retry_on_timeout=False,
            plan_fallback_reasoner="",
            plan_fallback_timeout=0,
            goals="PROJECT_GOALS.md",
            extra_context=[],
            plan_out="",
            test_cmd="python3 -m compileall src main.py",
            lint_cmd="",
            _config_path=None,
        )

    @mock.patch("molde_maestro.pipeline.preflight")
    @mock.patch("molde_maestro.pipeline.call_model")
    def test_cmd_plan_saves_prompt_raw_and_metadata(
        self,
        mock_call_model: mock.Mock,
        _mock_preflight: mock.Mock,
    ) -> None:
        mock_call_model.return_value = "Thinking...\n# Repo Review Plan\n\n## Summary\n- ok\n"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "PROJECT_GOALS.md").write_text("- meta\n", encoding="utf-8")
            args = self.make_plan_args(repo)

            pipeline.cmd_plan(args)

            ai_dir = repo / "AI"
            self.assertTrue((ai_dir / "plan-prompt.txt").exists())
            self.assertTrue((ai_dir / "plan-raw.txt").exists())
            self.assertTrue((ai_dir / "plan.md").exists())
            metadata = json.loads((ai_dir / "run-metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["stages"][0]["name"], "plan")
            self.assertEqual(metadata["stages"][0]["status"], "ok")
            self.assertEqual(metadata["stages"][0]["details"]["selected_attempt"], 1)
            self.assertEqual(metadata["stages"][0]["details"]["selected_mode"], "balanced")
            self.assertTrue((ai_dir / "plan.md").read_text(encoding="utf-8").startswith("# Repo Review Plan"))
            attempts = json.loads((ai_dir / "plan-attempts.json").read_text(encoding="utf-8"))
            self.assertEqual(attempts[0]["status"], "ok")

    @mock.patch("molde_maestro.pipeline.preflight")
    @mock.patch("molde_maestro.pipeline.call_model")
    def test_cmd_plan_records_failure_artifact(
        self,
        mock_call_model: mock.Mock,
        _mock_preflight: mock.Mock,
    ) -> None:
        mock_call_model.side_effect = pipeline.ExecutionFailure(
            "boom",
            command="ollama run deepseek-r1",
            status="timeout",
            timeout_seconds=9,
            stderr="slow",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "PROJECT_GOALS.md").write_text("- meta\n", encoding="utf-8")
            ai_dir = repo / "AI"
            ai_dir.mkdir()
            (ai_dir / "plan.md").write_text("stale", encoding="utf-8")
            args = self.make_plan_args(repo)

            with self.assertRaises(pipeline.ExecutionFailure):
                pipeline.cmd_plan(args)

            self.assertTrue((ai_dir / "plan-prompt.txt").exists())
            self.assertTrue((ai_dir / "plan-error.md").exists())
            self.assertFalse((ai_dir / "plan.md").exists())
            metadata = json.loads((ai_dir / "run-metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "timeout")
            self.assertEqual(metadata["error"]["details"]["stage"], "plan")

    @mock.patch("molde_maestro.pipeline.preflight")
    @mock.patch("molde_maestro.pipeline.call_model")
    def test_cmd_plan_retries_with_fast_fallback(
        self,
        mock_call_model: mock.Mock,
        _mock_preflight: mock.Mock,
    ) -> None:
        mock_call_model.side_effect = [
            pipeline.ExecutionFailure("slow", status="timeout", timeout_seconds=5, stdout="thinking"),
            "# Repo Review Plan\n\n## Summary\n- fallback\n",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "PROJECT_GOALS.md").write_text("- meta\n", encoding="utf-8")
            args = self.make_plan_args(repo)
            args.plan_retry_on_timeout = True

            pipeline.cmd_plan(args)

            ai_dir = repo / "AI"
            attempts = json.loads((ai_dir / "plan-attempts.json").read_text(encoding="utf-8"))
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0]["status"], "timeout")
            self.assertEqual(attempts[1]["mode"], "fast")
            self.assertTrue((ai_dir / "plan-raw.txt").exists())

    @mock.patch("molde_maestro.pipeline.preflight")
    @mock.patch("molde_maestro.pipeline.call_model")
    def test_cmd_report_writes_grounded_report_when_model_fails(
        self,
        mock_call_model: mock.Mock,
        _mock_preflight: mock.Mock,
    ) -> None:
        mock_call_model.side_effect = pipeline.ExecutionFailure("reviewer offline", status="timeout")
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            ai_dir = repo / "AI"
            ai_dir.mkdir()
            (repo / "PROJECT_GOALS.md").write_text("- keep stable\n", encoding="utf-8")
            (ai_dir / "plan.md").write_text("# Repo Review Plan\n\n1) Change: Fix foo\n", encoding="utf-8")
            (ai_dir / "test-report.md").write_text("- stage_status: **ok**\n", encoding="utf-8")
            (ai_dir / "run-metadata.json").write_text(json.dumps({"status": "ok", "stages": []}), encoding="utf-8")
            args = argparse.Namespace(
                repo=str(repo),
                ai_dir="AI",
                cmd="report",
                goals="PROJECT_GOALS.md",
                reasoner="ollama:deepseek-r1",
                base_ref="HEAD~1",
                report_timeout=15,
                reasoner_timeout=15,
                _config_path=None,
            )

            with mock.patch("molde_maestro.pipeline.collect_git_diff", return_value="diff --git a/src/foo.py b/src/foo.py"):
                with mock.patch("molde_maestro.pipeline.collect_git_changed_files", return_value=["src/foo.py"]):
                    pipeline.cmd_report(args)

            self.assertTrue((ai_dir / "final.md").exists())
            self.assertFalse((ai_dir / "report.md").exists())
            metadata = json.loads((ai_dir / "report-command-metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "ok")
            report_stage = metadata["stages"][0]
            self.assertEqual(report_stage["details"]["model_summary_status"], "failed")
            self.assertTrue((ai_dir / "report-model-error.md").exists())

    @mock.patch("molde_maestro.pipeline.preflight")
    @mock.patch("molde_maestro.pipeline.call_model", return_value="# Final Report\n\nModel summary\n")
    def test_cmd_report_skips_optional_model_when_reasoner_missing(
        self,
        mock_call_model: mock.Mock,
        _mock_preflight: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            ai_dir = repo / "AI"
            ai_dir.mkdir()
            (repo / "PROJECT_GOALS.md").write_text("- keep stable\n", encoding="utf-8")
            (ai_dir / "plan.md").write_text("# Repo Review Plan\n\n1) Change: Fix foo\n", encoding="utf-8")
            (ai_dir / "test-report.md").write_text("- stage_status: **ok**\n", encoding="utf-8")
            (ai_dir / "run-metadata.json").write_text(json.dumps({"status": "ok", "stages": []}), encoding="utf-8")
            args = argparse.Namespace(
                repo=str(repo),
                ai_dir="AI",
                cmd="report",
                goals="PROJECT_GOALS.md",
                reasoner="",
                base_ref="HEAD~1",
                report_timeout=15,
                reasoner_timeout=15,
                _config_path=None,
            )

            with mock.patch("molde_maestro.pipeline.collect_git_diff", return_value="diff --git a/src/foo.py b/src/foo.py"):
                with mock.patch("molde_maestro.pipeline.collect_git_changed_files", return_value=["src/foo.py"]):
                    pipeline.cmd_report(args)

            mock_call_model.assert_not_called()
            metadata = json.loads((ai_dir / "report-command-metadata.json").read_text(encoding="utf-8"))
            report_stage = metadata["stages"][0]
            self.assertEqual(report_stage["details"]["model_summary_status"], "skipped")

    @mock.patch("molde_maestro.pipeline.preflight")
    def test_cmd_run_relies_on_validation_plan_instead_of_raw_test_cmd(self, _mock_preflight: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            (repo / "PROJECT_GOALS.md").write_text("- keep stable\n", encoding="utf-8")
            args = argparse.Namespace(
                repo=str(repo),
                ai_dir="AI",
                cmd="run",
                goals="PROJECT_GOALS.md",
                reasoner="ollama:deepseek-r1",
                aider_model="ollama:qwen3-coder:14b",
                test_cmd="",
                lint_cmd="",
                lint_required=False,
                max_iters=1,
                zip=False,
                base="",
                branch="",
                aider_timeout=15,
                test_timeout=15,
                report_timeout=15,
                reasoner_timeout=15,
                plan_mode="balanced",
                extra_context=[],
                aider_extra_arg=[],
                apply_limit_to_plan_files=True,
                apply_enforce_plan_scope=True,
                apply_skip_unsupported_plan_changes=False,
                allowed_file=[],
                apply_max_plan_changes=0,
                base_ref="",
                _config_path=None,
            )
            validation_plan = pipeline.ValidationPlan(
                profile="python_local",
                source="auto",
                suggested_profile="python_local",
                suggestion_reason="reason",
                promotion_decision="not_promoted",
                promotion_reasons=[],
                promotion_blockers=[],
                test_command="python3 -m compileall src",
                lint_command=None,
                semantic_validation=False,
                semantic_validation_mode="auto",
                semantic_validation_strict=False,
                semantic_validation_timeout=60,
                smoke_imports=False,
                smoke_imports_strict=False,
                smoke_import_runner=None,
                checks=[],
                context=pipeline.ValidationContext(
                    is_python_repo=True,
                    has_tests=False,
                    has_pyproject=True,
                    has_requirements=False,
                    has_src_dir=True,
                    has_main_py=False,
                    has_dot_venv=False,
                    has_uv_lock=False,
                    has_setup_cfg=False,
                    python_runner="python3",
                    python_runner_source="system",
                    runner_reproducible=False,
                    runner_usable=True,
                    pytest_available=False,
                    pytest_command="",
                    compile_command="python3 -m compileall src",
                    repo_notes=[],
                    suggested_profile="python_local",
                    suggestion_reason="reason",
                    promotion_reasons=[],
                    promotion_blockers=[],
                ),
            )

            with mock.patch("molde_maestro.pipeline.resolve_validation_plan", return_value=validation_plan):
                with mock.patch("molde_maestro.pipeline.run_plan_generation", side_effect=RuntimeError("stop after validation")):
                    with self.assertRaisesRegex(RuntimeError, "stop after validation"):
                        pipeline.cmd_run(args)


class TestReportTests(unittest.TestCase):
    def test_write_test_report_marks_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            ai_dir = repo / "AI"
            ai_dir.mkdir()
            with mock.patch(
                "molde_maestro.pipeline.run_cmd",
                side_effect=subprocess_timeout_side_effect,
            ):
                passed, _report_md, summary = pipeline.write_test_report(
                    repo=repo,
                    ai_dir=ai_dir,
                    lint_cmd=None,
                    test_cmd="python3 slow.py",
                    lint_required=False,
                    timeout=3,
                    changed_files=[],
                    semantic_validation=False,
                )
            self.assertFalse(passed)
            self.assertEqual(summary["status"], "timeout")
            report = (ai_dir / "test-report.md").read_text(encoding="utf-8")
            self.assertIn("Timeout seconds", report)

    def test_write_test_report_fails_on_semantic_validation_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            ai_dir = repo / "AI"
            ai_dir.mkdir()
            (repo / "src/pkg").mkdir(parents=True)
            (repo / "src/pkg/mod.py").write_text(
                "def crop_region(image, region):\n    return image\n\n\ndef preprocess_page(image):\n    return crop_region(image)\n",
                encoding="utf-8",
            )
            with mock.patch("molde_maestro.pipeline.run_cmd", return_value=(0, "ok", "")):
                passed, report_md, summary = pipeline.write_test_report(
                    repo=repo,
                    ai_dir=ai_dir,
                    lint_cmd=None,
                    test_cmd="python3 -m compileall src",
                    lint_required=False,
                    timeout=3,
                    changed_files=["src/pkg/mod.py"],
                    semantic_validation=True,
                    semantic_validation_mode="ast",
                    semantic_validation_strict=True,
                    semantic_validation_timeout=5,
                )
            self.assertFalse(passed)
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["semantic_validation"]["status"], "failed")
            self.assertEqual(summary["checks"][0]["name"], "test_command")
            self.assertEqual(summary["validation_profile"]["profile"], "manual")
            self.assertIn("call_arity_mismatch", report_md)

    def test_aider_output_has_fatal_error_detects_provider_issue(self) -> None:
        self.assertTrue(
            pipeline.aider_output_has_fatal_error(
                "litellm.BadRequestError: LLM Provider NOT provided",
                "",
            )
        )
        self.assertFalse(pipeline.aider_output_has_fatal_error("all good", ""))


def subprocess_timeout_side_effect(*args, **kwargs):
    raise pipeline.subprocess.TimeoutExpired(cmd="python3 slow.py", timeout=3, output="out", stderr="err")


if __name__ == "__main__":
    unittest.main()
