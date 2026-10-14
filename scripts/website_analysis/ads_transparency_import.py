"""Import a completed saved domain scan into existing company_market columns.

No network requests or schema migrations. Detailed creatives and completeness
remain in the source evidence. The version makes this import repeatable.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


DEFAULT_VERSION = 'ads-transparency-2026-10-14'


def nullable_bool(value):
    return None if value is None else int(bool(value))


def import_scan(conn: sqlite3.Connection, output: Path, market_version: str) -> dict[str, int]:
    companies = json.loads((output / 'companies.json').read_text(encoding='utf-8'))
    summary = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    domains = {r['domain']: r for line in (output / 'domains.jsonl').read_text(encoding='utf-8').splitlines()
               if line.strip() for r in [json.loads(line)]}
    if summary.get('phase') != 'complete':
        raise ValueError('Only a completed scan can be imported')
    if not market_version or len(companies) != summary['population_companies'] or len(domains) != summary['checked_domains']:
        raise ValueError('Source counts or market version are invalid')
    numbers = [c['company_number'] for c in companies]
    if len(set(numbers)) != len(numbers):
        raise ValueError('Duplicate company identifiers')
    rows = []
    counts = {'companies': len(companies), 'with_ads': 0, 'no_ads': 0, 'unknown': 0}
    for company in companies:
        number = company['company_number']
        if not conn.execute('select 1 from companies where company_number=?', (number,)).fetchone():
            raise ValueError(f'Company {number} is not present in the database')
        result = domains.get(company['domain']) if not company['skip_reason'] else None
        known = result is not None and result['status'] in ('complete', 'partial') and (result['creatives'] or result['complete'])
        ads = result['ads'] if known else {}
        count = ads.get('ads_seen')
        if known and (not isinstance(count, int) or count < 0 or bool(count) != bool(result['creatives'])):
            raise ValueError(f'Invalid ad count for {number}')
        counts['unknown' if not known else 'with_ads' if result['creatives'] else 'no_ads'] += 1
        timestamp = result['checked_at'] if result else summary['updated_at']
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            raise ValueError('Assessment timestamp must include a timezone')
        timestamp = parsed.astimezone(timezone.utc).isoformat()
        rows.append((number, market_version, company['domain'] if not company['skip_reason'] else None,
                     count, nullable_bool(ads.get('advertising_now')), ads.get('first_shown'), ads.get('last_shown'),
                     nullable_bool(ads.get('verified')) if known and result['creatives'] else None,
                     (ads.get('advertisers') or [None])[0], timestamp))
    with conn:
        conn.executemany(
            'insert into company_market (company_number,market_version,domain,ads_seen,advertising_now,'
            'ads_first_shown,ads_last_shown,advertiser_verified,advertiser_name,assessed_at) '
            'values (?,?,?,?,?,?,?,?,?,?) on conflict(company_number,market_version) do update set '
            'domain=excluded.domain,ads_seen=excluded.ads_seen,advertising_now=excluded.advertising_now,'
            'ads_first_shown=excluded.ads_first_shown,ads_last_shown=excluded.ads_last_shown,'
            'advertiser_verified=excluded.advertiser_verified,advertiser_name=excluded.advertiser_name,'
            'assessed_at=excluded.assessed_at', rows)
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--db', type=Path, default=Path('companies-house.db'))
    parser.add_argument('--market-version', default=DEFAULT_VERSION)
    args = parser.parse_args()
    # mode=rw refuses to create an accidental empty database at a wrong path.
    with sqlite3.connect(args.db.resolve().as_uri() + '?mode=rw', uri=True) as conn:
        print(json.dumps(import_scan(conn, args.output, args.market_version)))


if __name__ == '__main__':
    main()
