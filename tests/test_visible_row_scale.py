import pytest

from core.companies_house_extractor import CompaniesHouseExtractor, display_scale, prefer_exact


def _page(unit_header: str, turnover=("1,288", "1,292"), kpi_first: bool = False) -> str:
    kpi = ("<div>Key performance indicators</div><div>£'000</div><div class=\"crn fn1\">£'000</div>"
           "<div class=\"cln\">Turnover</div><div class=\"crn fn1\">28,815</div><div class=\"crn fn1\">24,225</div>") if kpi_first else ""
    return (f"<html><body>{kpi}<div>Profit and loss account</div><div class=\"crb fn1\">2024</div>"
            f"<div class=\"crb fn1\">2023</div><div class=\"crn fn1\">{unit_header}</div>"
            f"<div class=\"crn fn1\">{unit_header}</div><div class=\"cln\">Turnover</div>"
            f"<div class=\"crn fn1\">{turnover[0]}</div><div class=\"crn fn1\">{turnover[1]}</div>"
            f"<div class=\"cln\">Gross profit</div><div class=\"crn fn1\">300</div><div class=\"crn fn1\">250</div></body></html>")


EX = CompaniesHouseExtractor(api_key=None)


@pytest.mark.parametrize("text, expected", [
    ("Profit and loss account £'000 £'000", 1000),
    ("Profit and loss account £000 £000", 1000),
    ("Profit and loss account £\xa0000", 1000),
    ("amounts in £m", 1_000_000),
    ("amounts in thousands", 1000),
    ("Profit and loss account £ £", 1),
    ("no unit anywhere", 1),
    ("an earlier table in £'000 then, later, the real statement £ £", 1),     # nearest marker wins
    ("dividends were paid amounting to £238,300 and turnover rose", 1),        # a figure in prose is not a unit
])
def test_display_scale(text, expected):
    assert display_scale(text) == expected


def test_a_thousands_statement_is_scaled_to_pounds():
    row = EX._extract_visible_two_column_row(_page("£'000"), "Turnover")
    assert row == {"current": 1_288_000, "previous": 1_292_000}


def test_a_statement_in_pounds_is_left_alone():
    row = EX._extract_visible_two_column_row(_page("£", turnover=("1,288,000", "1,292,000")), "Turnover")
    assert row == {"current": 1_288_000, "previous": 1_292_000}


def test_the_row_under_the_profit_and_loss_heading_beats_an_earlier_kpi_table():
    row = EX._extract_visible_two_column_row(_page("£", turnover=("28,814,934", "24,225,227"), kpi_first=True), "Turnover")
    assert row == {"current": 28_814_934, "previous": 24_225_227}


def test_prefer_exact_uses_the_unrounded_tag_when_they_agree_to_half_a_percent():
    assert prefer_exact(28_815_000, 28_814_934) == 28_814_934
    assert prefer_exact(1_288_000, 1_288_000) == 1_288_000


def test_prefer_exact_keeps_the_printed_row_when_they_really_differ():
    assert prefer_exact(1_000_000, 2_000_000) == 1_000_000


def test_prefer_exact_falls_back_when_one_side_is_missing():
    assert prefer_exact(None, 5) == 5
    assert prefer_exact(7, None) == 7
    assert prefer_exact(None, None) is None
