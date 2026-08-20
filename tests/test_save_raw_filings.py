from scripts.profile.save_raw_filings import to_readable_text


def test_to_readable_text_breaks_paragraphs_onto_separate_lines():
    xhtml = "<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
    assert to_readable_text(xhtml).splitlines() == ["First paragraph.", "Second paragraph."]


def test_to_readable_text_breaks_table_cells_onto_separate_lines():
    xhtml = "<table><tr><td>Turnover</td><td>13,391,763</td></tr></table>"
    assert to_readable_text(xhtml).splitlines() == ["Turnover", "13,391,763"]


def test_to_readable_text_drops_ixbrl_header_metadata():
    xhtml = (
        "<html><body>"
        '<ix:header xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
        "<ix:hidden>context-and-unit-junk 2024-01-01</ix:hidden>"
        "</ix:header>"
        "<p>Principal activity was dispensing chemists.</p>"
        "</body></html>"
    )
    text = to_readable_text(xhtml)
    assert "context-and-unit-junk" not in text
    assert "Principal activity was dispensing chemists." in text


def test_to_readable_text_drops_head_style_and_script_blocks():
    xhtml = (
        "<html><head><style>.a{color:red}</style>"
        "<script>var x = 1;</script></head>"
        "<body><p>Visible text.</p></body></html>"
    )
    text = to_readable_text(xhtml)
    assert "color:red" not in text
    assert "var x" not in text
    assert text == "Visible text."


def test_to_readable_text_unescapes_entities_and_collapses_inline_whitespace():
    xhtml = "<p>Fish &amp;   chips</p>"
    assert to_readable_text(xhtml) == "Fish & chips"


def test_to_readable_text_drops_blank_lines():
    xhtml = "<div><p></p><p>Only this survives.</p><div>   </div></div>"
    assert to_readable_text(xhtml).splitlines() == ["Only this survives."]
