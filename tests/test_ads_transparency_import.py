import json
import sqlite3

import pytest

from companies_house_core.companies_house_sqlite import init_db
from scripts.website_analysis.ads_transparency_import import import_scan


def fixture_scan(tmp_path):
    rows = [{'company_number': '00000001', 'domain': 'shared.example', 'skip_reason': None},
            {'company_number': '00000002', 'domain': 'shared.example', 'skip_reason': None},
            {'company_number': '00000003', 'domain': 'empty.example', 'skip_reason': None},
            {'company_number': '00000004', 'domain': None, 'skip_reason': 'no_chosen_website'}]
    (tmp_path/'companies.json').write_text(json.dumps(rows))
    positive = {'domain': 'shared.example', 'status': 'partial', 'complete': False,
                'creatives': [{}], 'checked_at': '2026-10-14T12:00:00+00:00',
                'ads': {'ads_seen': 40, 'advertising_now': None, 'verified': None,
                        'advertisers': ['Example advertiser'], 'first_shown': '2026-01-01',
                        'last_shown': None}}
    empty = {'domain': 'empty.example', 'status': 'complete', 'complete': True,
             'creatives': [], 'checked_at': '2026-10-14T12:01:00+00:00',
             'ads': {'ads_seen': 0, 'advertising_now': False, 'verified': False}}
    (tmp_path/'domains.jsonl').write_text('\n'.join(json.dumps(r) for r in [positive, empty]))
    (tmp_path/'summary.json').write_text(json.dumps({'phase': 'complete', 'updated_at': '2026-10-14T13:00:00+00:00',
                                                   'population_companies': 4, 'checked_domains': 2}))
    return rows


def fixture_db():
    conn = sqlite3.connect(':memory:')
    init_db(conn)
    for n in range(1, 5):
        conn.execute("insert into companies (company_number,company_name,profile_payload,updated_at) values (?,?,?,?)", (f'{n:08d}', 'Example', '{}', '2026-10-14'))
    conn.commit()
    return conn


def test_import_fans_out_preserves_unknowns_and_is_idempotent(tmp_path):
    fixture_scan(tmp_path)
    conn = fixture_db()
    summary = import_scan(conn, tmp_path, 'scan-test')
    assert summary == {'companies': 4, 'with_ads': 2, 'no_ads': 1, 'unknown': 1}
    assert conn.execute('select ads_seen,advertising_now,advertiser_verified from company_market where company_number="00000001"').fetchone() == (40, None, None)
    assert conn.execute('select ads_seen,advertising_now,advertiser_verified from company_market where company_number="00000003"').fetchone() == (0, 0, None)
    assert conn.execute('select ads_seen,advertising_now,domain,assessed_at from company_market where company_number="00000004"').fetchone() == (None, None, None, '2026-10-14T13:00:00+00:00')
    conn.execute('update company_market set phrase_search_volume=100, gap_count=3 where company_number="00000001"')
    conn.commit()
    import_scan(conn, tmp_path, 'scan-test')
    assert conn.execute('select count(*) from company_market').fetchone()[0] == 4
    assert conn.execute('select phrase_search_volume,gap_count from company_market where company_number="00000001"').fetchone() == (100, 3)


def test_missing_company_causes_no_partial_import(tmp_path):
    fixture_scan(tmp_path)
    conn = fixture_db()
    conn.execute('delete from companies where company_number="00000004"')
    conn.commit()
    with pytest.raises(ValueError, match='not present'):
        import_scan(conn, tmp_path, 'scan-test')
    assert conn.execute('select count(*) from company_market').fetchone()[0] == 0


def test_incomplete_source_is_rejected(tmp_path):
    fixture_scan(tmp_path)
    (tmp_path/'summary.json').write_text(json.dumps({'phase': 'running'}))
    with pytest.raises(ValueError, match='completed'):
        import_scan(fixture_db(), tmp_path, 'scan-test')
