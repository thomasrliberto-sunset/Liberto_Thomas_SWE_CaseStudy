from datetime import date
from pathlib import Path

from app.ingest.insiders import parse_form4, raw_xml_document
from app.ingest.sections import chunk_section, extract_risk_factors, extract_sections

FIXTURES = Path(__file__).parent / "fixtures"


def sections():
    return extract_sections((FIXTURES / "tenk_sample.html").read_text(), ("1A", "7"))


def test_item_1a_skips_table_of_contents_and_page_furniture():
    s = sections()["1A"]
    assert s.method == "item-heading"
    assert s.lines[0].text.startswith("The following risk factors")
    assert all(line.text != "17" for line in s.lines)  # page number dropped
    assert "hidden xbrl" not in s.text
    assert "Unresolved Staff Comments" not in s.text  # stops at the next Item


def test_item_7_cross_reference_falls_back_to_titled_section():
    s = sections()["7"]
    assert s.method == "title-heading"
    assert "Net sales increased 10%" in s.text
    assert "Net sales | $27,448 | $24,878 | 10%" in s.text  # table row, $ and % cells glued back on
    assert "Pursuant to the requirements" not in s.text  # stops at SIGNATURES


def test_risk_factors_handle_standalone_and_run_in_headings():
    factors = extract_risk_factors(sections()["1A"])
    headings = [f.heading for f in factors]
    assert headings == [
        "Competition could adversely impact our market share and financial results.",
        "Export controls may restrict our ability to sell products to certain customers.",  # run-in (MSFT style)
        "Our stock price may be volatile and could decline significantly.",
    ]
    # the emphasized intro sentence has no body, so it is not a risk factor
    assert factors[0].category == "Risks Related to Our Industry"
    # a plain subheading stays inside its risk factor instead of starting a new one
    assert "Competition in the datacenter market" in factors[0].body
    assert factors[1].body.startswith("The U.S. government has imposed")
    assert factors[2].category == "General Risk Factors"


def test_chunks_carry_heading_paths():
    chunks = chunk_section(sections()["7"])
    assert chunks[0].heading == "RESULTS OF OPERATIONS"
    risk_chunks = chunk_section(sections()["1A"])
    assert any(c.heading and c.heading.startswith("Risks Related to Our Industry > Competition could") for c in risk_chunks)


def test_form4_parsing_and_10b5_1_detection():
    txs = parse_form4((FIXTURES / "form4_sample.xml").read_text())
    assert len(txs) == 2  # holdings rows are not transactions
    sale, withholding = txs
    assert sale.code == "S" and sale.shares == 1000 and sale.price == 180.5
    assert sale.transaction_date == date(2026, 3, 10)
    assert sale.is_10b5_1 is True  # via footnote
    assert withholding.code == "F" and withholding.is_10b5_1 is False
    assert sale.relationship == "Officer (EVP and CFO)"


def test_raw_xml_document_strips_xsl_prefix():
    assert raw_xml_document("xslF345X05/wk-form4_1.xml") == "wk-form4_1.xml"
    assert raw_xml_document("form4.xml") == "form4.xml"
