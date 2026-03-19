"""Tests for graph-informed navigation context."""

from pathlib import Path
from unittest.mock import MagicMock

from src.graph.context import _extract_keywords, build_navigation_context


class TestKeywordExtraction:
    def test_extracts_snake_case_identifiers(self):
        kws = _extract_keywords("Fix the run_agent function in dispatcher")
        assert "run_agent" in kws

    def test_extracts_camel_case_identifiers(self):
        kws = _extract_keywords("Update the WorkerDaemon class")
        assert "WorkerDaemon" in kws

    def test_extracts_file_paths(self):
        kws = _extract_keywords("Modify src/worker.py to add logging")
        assert "src/worker.py" in kws

    def test_extracts_dotted_names(self):
        kws = _extract_keywords("Call db.update_task with the new status")
        assert "db.update_task" in kws

    def test_filters_stopwords(self):
        kws = _extract_keywords("add a new function to the worker")
        assert "a" not in kws
        assert "the" not in kws
        assert "add" not in kws

    def test_empty_prompt_returns_empty(self):
        assert _extract_keywords("") == []
        assert _extract_keywords("   ") == []

    def test_caps_at_30_keywords(self):
        long_prompt = " ".join(f"identifier_{i}" for i in range(50))
        kws = _extract_keywords(long_prompt)
        assert len(kws) <= 30

    def test_deduplicates_case_insensitive(self):
        kws = _extract_keywords("Config config CONFIG")
        config_count = sum(1 for kw in kws if kw.lower() == "config")
        assert config_count == 1


class TestBuildNavigationContext:
    def _make_mock_store(self, search_results=None, impact_results=None):
        store = MagicMock()
        store.search_nodes.return_value = search_results or []
        store.get_impact_radius.return_value = impact_results or {
            "changed_nodes": [],
            "impacted_nodes": [],
            "impacted_files": [],
            "edges": [],
        }
        return store

    def _make_node(self, name, file_path, kind="Function", qualified_name=None):
        node = MagicMock()
        node.name = name
        node.file_path = file_path
        node.kind = kind
        node.qualified_name = qualified_name or f"{file_path}::{name}"
        node.line_start = 1
        node.line_end = 10
        node.is_test = False
        return node

    def test_returns_none_for_no_keywords(self):
        store = self._make_mock_store()
        result = build_navigation_context(store, "", Path("/tmp/repo"))
        assert result is None

    def test_returns_none_for_no_matches(self):
        store = self._make_mock_store(search_results=[])
        result = build_navigation_context(store, "fix the frobulator", Path("/tmp/repo"))
        assert result is None

    def test_returns_structured_dict_on_match(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        node = self._make_node("dispatch_task", str(src_dir / "dispatcher.py"))
        store = self._make_mock_store(
            search_results=[node],
            impact_results={
                "changed_nodes": [node],
                "impacted_nodes": [],
                "impacted_files": [],
                "edges": [],
            },
        )
        result = build_navigation_context(store, "fix dispatch_task", tmp_path)
        assert result is not None
        assert "matched_files" in result
        assert len(result["matched_files"]) > 0
        assert result["matched_files"][0]["path"] == "src/dispatcher.py"

    def test_includes_related_files_from_impact(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        matched = self._make_node("dispatch_task", str(src_dir / "dispatcher.py"))
        impacted = self._make_node("WorkerDaemon", str(src_dir / "worker.py"), kind="Class")

        store = self._make_mock_store(
            search_results=[matched],
            impact_results={
                "changed_nodes": [matched],
                "impacted_nodes": [impacted],
                "impacted_files": [str(src_dir / "worker.py")],
                "edges": [],
            },
        )
        result = build_navigation_context(store, "fix dispatch_task", tmp_path)
        assert result is not None
        assert len(result["related_files"]) > 0
        assert result["related_files"][0]["path"] == "src/worker.py"

    def test_handles_exception_gracefully(self):
        store = MagicMock()
        store.search_nodes.side_effect = RuntimeError("DB locked")
        result = build_navigation_context(store, "fix dispatch_task", Path("/tmp/repo"))
        assert result is None


class TestNavigationPromptTemplate:
    def test_template_renders_with_navigation(self):
        from src.dispatcher import AGENT_PROMPT_TEMPLATE

        result = AGENT_PROMPT_TEMPLATE.format(
            project_context="## Project Context\nTech stack: Python + pytest\n\n",
            learnings_section="## Learnings\n  [+] thing worked\n\n",
            navigation_section="## Navigation Context\nRelevant files:\n  - src/foo.py\n\n",
            task_prompt="Fix the bug",
        )
        assert "## Navigation Context" in result
        assert "## Task" in result
        assert "Fix the bug" in result

    def test_template_renders_without_navigation(self):
        from src.dispatcher import AGENT_PROMPT_TEMPLATE

        result = AGENT_PROMPT_TEMPLATE.format(
            project_context="",
            learnings_section="",
            navigation_section="",
            task_prompt="Fix the bug",
        )
        assert "## Navigation Context" not in result
        assert "## Task" in result
        assert "Fix the bug" in result
