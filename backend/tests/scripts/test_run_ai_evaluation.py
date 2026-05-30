from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "evals" / "scripts" / "run_ai_evaluation.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_ai_evaluation", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_evaluation_records_maps_answer_to_response() -> None:
    module = load_module()
    records = [
        {
            "case_id": "case-1",
            "query": "甘口でおすすめは？",
            "answer": "純米吟醸原酒 冬樹がおすすめです。",
            "ground_truth": "冬樹を候補にできる。",
            "expected_products": ["純米吟醸原酒 冬樹"],
            "must_include": ["冬樹"],
            "must_not_include": ["在庫ありと断定"],
            "source_refs": ["catalog.md#冬樹"],
        }
    ]

    eval_records = module.build_evaluation_records(records, shared_context="# catalog")

    assert eval_records == [
        {
            "case_id": "case-1",
            "category": None,
            "query": "甘口でおすすめは？",
            "response": "純米吟醸原酒 冬樹がおすすめです。",
            "context": (
                "## Store context\n# catalog\n\n## Case expectation\n"
                + json.dumps(
                    {
                        "ground_truth": "冬樹を候補にできる。",
                        "expected_products": ["純米吟醸原酒 冬樹"],
                        "must_include": ["冬樹"],
                        "must_not_include": ["在庫ありと断定"],
                        "evaluation_focus": [],
                        "source_refs": ["catalog.md#冬樹"],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            ),
            "ground_truth": "冬樹を候補にできる。",
            "expected_products": ["純米吟醸原酒 冬樹"],
            "evaluation_focus": [],
            "source_refs": ["catalog.md#冬樹"],
            "run_id": None,
            "agent_version": None,
            "model": None,
            "revision": None,
        }
    ]


def test_load_context_reads_supported_files(tmp_path: Path) -> None:
    module = load_module()
    (tmp_path / "catalog.md").write_text("# 酒カタログ\n冬樹", encoding="utf-8")
    (tmp_path / "ignore.bin").write_bytes(b"ignored")

    context = module.load_context(tmp_path, max_chars=1_000)

    assert "# 酒カタログ" in context
    assert "冬樹" in context
    assert "ignore.bin" not in context


def test_build_model_config_uses_foundry_project_base_endpoint() -> None:
    module = load_module()

    config = module.build_model_config(
        project_endpoint="https://example.services.ai.azure.com/api/projects/proj-default",
        deployment_name="gpt-5.4-nano",
    )

    assert config == {
        "azure_endpoint": "https://example.services.ai.azure.com",
        "azure_deployment": "gpt-5.4-nano",
    }


def test_build_model_config_prefers_explicit_azure_openai_endpoint() -> None:
    module = load_module()

    config = module.build_model_config(
        project_endpoint="https://example.services.ai.azure.com/api/projects/proj-default",
        azure_openai_endpoint="https://example.cognitiveservices.azure.com/",
        deployment_name="gpt-5.4-nano",
        api_key="test-key",
    )

    assert config == {
        "azure_endpoint": "https://example.cognitiveservices.azure.com",
        "azure_deployment": "gpt-5.4-nano",
        "api_key": "test-key",
    }


def test_filter_records_accepts_case_id_or_id() -> None:
    module = load_module()
    records = [{"case_id": "a"}, {"id": "b"}, {"case_id": "c"}]

    assert module.filter_records(records, ["a", "b"]) == records[:2]
