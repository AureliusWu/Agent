from __future__ import annotations

from app.tts.sentence_splitter import StreamingSentenceSplitter
from app.tts.text_normalizer import normalize_for_speech


def test_normalizer_removes_code_urls_paths_hashes_and_sensitive_values() -> None:
    drive_path = chr(67) + chr(58) + "\\secret\\file.txt"
    source = f"""# 标题\n正常说明。\n```python\nprint('secret')\n```\n网址 https://example.com/a，路径 {drive_path}，哈希 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa。API Key: top-secret"""
    result = normalize_for_speech(source)
    assert result.sensitive is True
    assert "正常说明" in result.text
    assert "print" not in result.text
    assert "https://" not in result.text
    assert drive_path not in result.text
    assert "aaaaaaaa" not in result.text
    assert "top-secret" not in result.text


def test_streaming_splitter_emits_complete_sentences_and_holds_code_fence() -> None:
    splitter = StreamingSentenceSplitter()
    assert splitter.feed("第一句还没") == []
    assert splitter.feed("结束。第二句！") == ["第一句还没结束。", "第二句！"]
    assert splitter.feed("```python\nprint(1)。") == []
    assert splitter.feed("\n```最后一句。") == ["```python\nprint(1)。\n```最后一句。"]


def test_flush_does_not_emit_unclosed_code() -> None:
    splitter = StreamingSentenceSplitter()
    splitter.feed("```json\n{\"x\": 1}")
    assert splitter.flush() == []


def test_streaming_splitter_hard_bounds_unpunctuated_provider_input() -> None:
    splitter = StreamingSentenceSplitter(max_chars=160)
    chunks = splitter.feed("长" * 401)
    chunks.extend(splitter.flush())

    assert "".join(chunks) == "长" * 401
    assert [len(chunk) for chunk in chunks] == [160, 160, 81]
