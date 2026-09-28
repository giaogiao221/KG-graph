from pathlib import Path

from book_engine.document.markdown_loader import MarkdownDocument
from book_engine.tables.table_detector import detect_tables


def test_detect_html_and_markdown_tables():
    text = """# 章节
表前条件温度为20℃。
<table><tr><td>A</td><td>1</td></tr></table>

| 材料 | 密度 |
|---|---|
| RDX | 1.81 |
"""
    doc = MarkdownDocument(Path("x.md"), text, text.splitlines())
    blocks = detect_tables(doc)
    assert len(blocks) == 2
    assert blocks[0].source_type == "html"
    assert blocks[0].heading == "章节"
    assert "20℃" in blocks[0].preceding_text
    assert blocks[1].source_type == "markdown"
