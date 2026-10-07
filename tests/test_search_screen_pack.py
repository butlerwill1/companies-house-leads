from __future__ import annotations

from scripts.search_screen_classifier import search_screen_pack as P

FILING = "\n".join([
    "Strategic report",
    "The company sells shoes to consumers through its own website and three shops.",
    "The directors are responsible for preparing the financial statements in accordance with the Companies Act 2006.",
    "The company operates in a competitive market and its brand is well known to customers.",
    "Turnover analysed by class of business: retail 4,000,000 wholesale 1,000,000",
    "bus:Director1 2024-01-01 2024-12-31 core:ShareCapital customers online",
    "Total 12,345",
])


def test_pack_keeps_informative_lines_and_drops_boilerplate_and_junk():
    pack = P.build_pack(FILING, "The principal activity is the sale of footwear.")
    assert "[principal activity] The principal activity is the sale of footwear." in pack
    assert "sells shoes to consumers through its own website" in pack
    assert "Companies Act" not in pack
    assert "bus:Director1" not in pack


def test_pack_includes_the_turnover_analysis_block():
    pack = P.build_pack(FILING)
    assert "[turnover analysis]" in pack and "class of business" in pack


def test_a_heading_only_principal_activity_is_not_repeated_as_content():
    pack = P.build_pack(FILING, "Principal activities")
    assert "[principal activity]" not in pack


def test_pack_is_bounded():
    long_filing = "\n".join(f"The company sells product {i} to customers online through retail shops." for i in range(400))
    pack = P.build_pack(long_filing, "x" * 5000)
    assert len(pack) < P.PRINCIPAL_ACTIVITY_LIMIT + P.LINES_LIMIT + P.TURNOVER_LIMIT + 200


def test_pack_lines_keep_document_order():
    filing = "\n".join([
        "The company sells goods to customers online through its website and shops.",
        "Filler line about nothing in particular that scores zero points at all.",
        "The group also provides services to clients through its branch network and website.",
    ])
    pack = P.build_pack(filing)
    assert pack.index("sells goods") < pack.index("provides services")


def test_a_strong_activity_statement_is_kept_even_with_one_evidence_class():
    filing = "\n".join([
        "Ninja Tune Limited is principally engaged in the production and exploitation of sound recordings.",
        "Nothing else of interest appears on this particular line at all today.",
    ])
    assert "principally engaged in the production" in P.build_pack(filing)


def test_the_principle_misspelling_is_treated_as_a_principal_activity_statement():
    filing = "The company's principle activity during the period was construction of homes for sale."
    assert "principle activity during the period was construction" in P.build_pack(filing)


def test_clean_full_text_drops_policy_boilerplate_and_numeric_rows_but_keeps_evidence():
    filing = "\n".join([
        "The company sells shoes to consumers through its own website.",
        "Recoverable amount is the higher of fair value less costs to sell and value in use of the asset.",
        "Revenue from online sales is recognised when the goods are delivered to the customer.",
        "Debtors 1,234 5,678 9,012 3,456",
        "1,234 5,678 9,012 3,456 7,890 1,234",
    ])
    cleaned, dropped = P.clean_full_text(filing)
    assert "sells shoes to consumers" in cleaned
    assert "Revenue from online sales is recognised" in cleaned  # boilerplate wording but names a channel and buyer
    assert "Recoverable amount" not in cleaned
    assert "1,234 5,678 9,012 3,456 7,890" not in cleaned
    assert dropped == {"boilerplate": 1, "numeric": 2} or dropped["boilerplate"] == 1


def test_clean_full_text_keeps_the_rows_after_a_turnover_heading():
    filing = "\n".join([
        "Turnover analysed by class of business",
        "Retail 4,000,000 3,000,000",
        "1,000,000 500,000",
        "Notes to the financial statements",
    ])
    cleaned, _ = P.clean_full_text(filing)
    assert "Retail 4,000,000" in cleaned and "1,000,000 500,000" in cleaned


def test_after_the_accounting_policies_only_evidence_lines_survive():
    filing = "\n".join([
        "The company sells shoes to consumers through its own website.",
        "Accounting convention",
        "The average monthly number of persons employed by the company during the year was thirty four.",
        "Directors remuneration was paid to the highest paid director during the year as disclosed.",
        "The ultimate parent undertaking is Shoe Holdings Limited, registered in England and Wales.",
    ])
    cleaned, dropped = P.clean_full_text(filing)
    assert "sells shoes to consumers" in cleaned
    assert "ultimate parent undertaking" in cleaned
    assert "average monthly number" not in cleaned and "highest paid director" not in cleaned
    assert dropped["notes"] == 3  # both non-evidence lines and the "Accounting convention" heading itself


def test_the_company_information_page_before_contents_is_dropped():
    filing = "\n".join([
        "ANNUAL REPORT AND FINANCIAL STATEMENTS", "Directors", "A Director", "Some Street", "Some Town", "AB1 2CD",
        "Auditor", "Audit LLP", "CONTENTS", "Page", "STRATEGIC REPORT",
        "The company sells shoes to consumers through its own website.",
    ])
    cleaned, dropped = P.clean_full_text(filing)
    assert "Some Street" not in cleaned and "sells shoes to consumers" in cleaned
    assert cleaned.startswith("ANNUAL REPORT") and dropped["front"] > 0


def test_standard_directors_report_paragraphs_are_dropped_unless_they_name_customers():
    filing = "\n".join([
        "So far as each person who was a director at the date of approving this report is aware, there is no relevant audit information.",
        "Applications for employment by disabled persons are always fully considered, bearing in mind the aptitudes of the applicant.",
        "The company sells shoes to consumers through its own website.",
    ])
    cleaned, _ = P.clean_full_text(filing)
    assert "So far as each person" not in cleaned and "disabled persons" not in cleaned
    assert "sells shoes" in cleaned
