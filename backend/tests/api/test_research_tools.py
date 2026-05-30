from types import SimpleNamespace

from src.api import research_tools


def test_extract_response_trace_marks_web_search_when_url_annotation_exists() -> None:
    """URL annotation が見える場合は、根拠付き web_search として追跡できる。"""
    response = SimpleNamespace(
        id="resp_research",
        conversation=SimpleNamespace(id="conv_research"),
        model="gpt-5.4-nano",
        tools=[SimpleNamespace(type="web_search")],
        output=[
            SimpleNamespace(
                content=[
                    SimpleNamespace(
                        annotations=[
                            SimpleNamespace(
                                type="url_citation",
                                title="山本 ピュアブラック",
                                url="https://example.test/pure-black",
                            )
                        ]
                    )
                ]
            )
        ],
        output_text="淡麗でキレのあるタイプです。",
    )

    trace = research_tools.extract_response_trace(response)

    assert trace["source"] == "web_search"
    assert trace["response_id"] == "resp_research"
    assert trace["conversation_id"] == "conv_research"
    assert trace["tool_types"] == ["web_search"]
    assert trace["source_urls"] == ["https://example.test/pure-black"]
    assert trace["annotations"][0]["title"] == "山本 ピュアブラック"


def test_extract_response_trace_marks_model_unknown_without_sources() -> None:
    """URL annotation がない場合は、検索根拠を確認できない状態として扱う。"""
    response = SimpleNamespace(
        id="resp_research",
        tools=[SimpleNamespace(type="web_search")],
        output=[],
        output_text="一般的には淡麗寄りです。",
    )

    trace = research_tools.extract_response_trace(response)

    assert trace["source"] == "model_unknown"
    assert trace["source_urls"] == []


def test_build_brand_research_prompt_requests_sources_and_preferred_query() -> None:
    """サブエージェントには優先検索語と根拠明記を求める。"""
    prompt = research_tools.build_brand_research_prompt(
        brand_name="山本ピュアブラック",
        user_context="淡麗な酒として好き",
    )

    assert "山本ピュアブラック 日本酒 味わい 甘辛 酸 香り 旨み 飲み口" in prompt
    assert "根拠URLまたはページ名" in prompt
    assert "根拠: 不明" in prompt
