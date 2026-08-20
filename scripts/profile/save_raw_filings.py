"""Save the raw XHTML filing (plus its Companies House document metadata)
for every business-profile gold-set case, so a label can be checked against
the filed text directly rather than through the extracted narrative.

Writes ch-data/business-profile-xhtml/<company_number>.xhtml and
<company_number>.metadata.json. ch-data/ is gitignored -- nothing here is
committed. Free document-API calls only, no model calls.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path

import requests

from core.companies_house_extractor import load_dotenv
from scripts.profile.business_profile_eval import case_files, load_case

DEST_DIR = Path("ch-data/business-profile-xhtml")


def _document_row(conn: sqlite3.Connection, company_number: str) -> sqlite3.Row | None:
    return conn.execute(
        "select document_id, transaction_id, metadata_url, xhtml_url, pdf_url, metadata_payload "
        "from documents where company_number=? and xhtml_url is not null "
        "order by rowid desc limit 1",
        (company_number,),
    ).fetchone()


def save_filing(conn: sqlite3.Connection, case: dict, api_key: str, dest_dir: Path) -> str:
    company_number = case["company_number"]
    row = _document_row(conn, company_number)
    if row is None:
        return "no_xhtml_doc"

    response = requests.get(
        f"https://document-api.company-information.service.gov.uk/document/{row['document_id']}/content",
        auth=(api_key, ""),
        headers={"Accept": "application/xhtml+xml"},
        timeout=60,
    )
    if response.status_code != 200:
        return f"http_{response.status_code}"

    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / f"{company_number}.xhtml").write_text(response.text, encoding="utf-8")

    metadata = {
        "company_number": company_number,
        "company_name": case.get("company_name"),
        "document_id": row["document_id"],
        "transaction_id": row["transaction_id"],
        "metadata_url": row["metadata_url"],
        "xhtml_url": row["xhtml_url"],
        "pdf_url": row["pdf_url"],
        "document_metadata": json.loads(row["metadata_payload"]) if row["metadata_payload"] else None,
        "fetched_from": f"https://document-api.company-information.service.gov.uk/document/{row['document_id']}/content",
    }
    (dest_dir / f"{company_number}.metadata.json").write_text(
        json.dumps(metadata, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return "ok"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="companies-house.db")
    parser.add_argument("--cases-dir", default="evals/business_profiles/cases")
    parser.add_argument("--dest-dir", default=str(DEST_DIR))
    args = parser.parse_args()

    load_dotenv(Path(".env"))
    api_key = os.environ["COMPANIES_HOUSE_API_KEY"]
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    dest_dir = Path(args.dest_dir)

    results: dict[str, str] = {}
    for path in case_files(Path(args.cases_dir)):
        case = load_case(path)
        results[case["company_number"]] = save_filing(conn, case, api_key, dest_dir)

    ok = sum(1 for status in results.values() if status == "ok")
    print(json.dumps({"saved": ok, "total": len(results)}, indent=2))
    for company_number, status in results.items():
        if status != "ok":
            print(f"  {company_number}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
