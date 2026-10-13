"""Offline checks for exact latest-year selection, fill scope and spend limits."""
import sqlite3
import json
from types import SimpleNamespace

import pytest

from companies_house_core.companies_house_sqlite import init_db
from scripts.pdf_vision_extraction import xhtml_recovery_batch as B


def test_latest_screened_selection_does_not_fall_back_to_older_complete_year():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for number in ('1', '2'):
        conn.execute("insert into companies(company_number,company_name,profile_payload,updated_at) "
                     "values (?,?,'{}','2026')", (number, 'Test company'))
        conn.execute("insert into documents(document_id,company_number,xhtml_url) values (?,?,?)",
                     ('D'+number, number, 'https://example.com/filing'))
    conn.execute("insert into company_search_screen(company_number,prompt_version,model,input_kind,passes,screened_at) "
                 "values ('1','v','m','short',1,'2026')")
    conn.executemany("insert into financial_period_summaries(company_number,document_id,period_type,financial_year,"
                     "turnover,profit_after_tax,raw_payload) values (?,?,?,?,?,?,'{}')", [
        ('1','OLD','current',2024,100,10), ('1','D1','current',2025,None,20),
        ('1','D1','previous',2024,None,None), ('2','D2','current',2025,None,None)])
    rows = B.latest_screened_gaps(conn)
    assert len(rows) == 1
    assert (rows[0]['company_number'], rows[0]['document_id'], rows[0]['financial_year']) == ('1','D1',2025)


def test_only_selected_period_is_recovered_and_wrong_date_is_rejected():
    result = {'status':'recovered', 'periods':{
        'current': {'period_end':'2025-12-31','turnover':{'status':'recovered'},'profit_after_tax':{'status':'absent'}},
        'previous': {'turnover':{'status':'recovered'}}}}
    doc = {'period_type':'current','period_end_on':'2025-12-31'}
    assert list(B.limit_to_selected_period(result,doc)['periods']) == ['current']
    wrong = B.limit_to_selected_period(result,{**doc,'period_end_on':'2024-12-31'})
    assert wrong['status'] == 'unresolved'
    assert wrong['periods']['current']['turnover']['status'] == 'rejected'


def test_budget_reserves_a_request_before_it_can_be_sent_and_never_retries():
    config={'max_output_tokens':2048,'input_price_per_million':0.75,'output_price_per_million':4.5,'model':'m'}
    journal=[]
    sent=[]
    caller=B.BoundedCall('key',config,B.SpendBudget(0.001),journal.append,lambda **kw: sent.append(kw))
    with pytest.raises(B.BudgetExhausted): caller('prompt')
    assert sent == [] and journal == []
    budget=B.SpendBudget(1)
    def fail(**kw):
        sent.append(kw)
        raise TimeoutError('failed')
    caller=B.BoundedCall('key',config,budget,journal.append,fail)
    with pytest.raises(TimeoutError): caller('prompt')
    assert len(sent) == 1 and journal[0]['kind'] == 'request_started'
    assert budget.spent > 0
    assert sent[0]['json']['max_tokens'] == 2048
    assert sent[0]['json']['provider']['max_price']['completion'] == 4.5


def test_resume_accounts_for_completed_and_unfinished_paid_requests():
    records=[{'kind':'request_started','key':'a','reserved_usd':0.02},
             {'kind':'completed','key':'a','cost_usd':0.003},
             {'kind':'request_started','key':'b','reserved_usd':0.04},
             {'kind':'completed','key':'c','cost_usd':0}]
    completed,pending,spent=B.resume_state(records)
    assert set(completed) == {'a','c'}
    assert pending == {'b'}
    assert spent == pytest.approx(0.043)


def test_replay_fills_only_selected_period_once_and_counts_actual_improvement():
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    init_db(conn)
    conn.executemany("insert into financial_period_summaries(company_number,document_id,period_type,financial_year,"
                     "turnover,raw_payload) values ('1','D1',?,?,100,'{}')", [('current',2025),('previous',2024)])
    summary_id=conn.execute("select id from financial_period_summaries where period_type='current'").fetchone()[0]
    doc={'company_number':'1','document_id':'D1','period_type':'current','financial_summary_id':summary_id,
         'turnover':100,'profit_after_tax':None}
    figure={'status':'recovered','reported_value':'50','currency_code':'GBP','displayed':'50',
            'unit':'GBP','scale_multiplier':1,'label':'Profit for the year','quote':'Profit for the year 50'}
    result=B.limit_to_selected_period({'status':'recovered','periods':{
        'current':{'profit_after_tax':figure},'previous':{'profit_after_tax':figure}}},doc)
    record={'document':doc,'result':result,'cost_usd':0.001}
    assert B.apply_completed(conn,record,'m') == {'turnover':0,'profit_after_tax':1}
    assert B.apply_completed(conn,record,'m') == {'turnover':0,'profit_after_tax':0}
    assert conn.execute("select profit_after_tax from financial_period_summaries where period_type='previous'").fetchone()[0] is None
    assert conn.execute('select count(*) from vlm_financial_extraction_runs').fetchone()[0] == 1
    assert B.coverage_summary(conn,[doc]) == {'companies_improved':1,'turnover_cells_filled':0,
        'profit_cells_filled':1,'with_both_before':0,'with_both_after':1}


def test_free_revalidation_preserves_paid_cost_and_fills_quoted_dash(tmp_path,monkeypatch):
    conn=sqlite3.connect(':memory:')
    conn.row_factory=sqlite3.Row
    init_db(conn)
    conn.execute("insert into financial_period_summaries(company_number,document_id,period_type,financial_year,"
                 "profit_after_tax,raw_payload) values ('1','D1','current',2025,5,'{}')")
    doc={'company_number':'1','document_id':'D1','period_type':'current'}
    payload={'unit':'GBP','periods':{'current':{'turnover':{
        'label':'Turnover','displayed':'-','quote':'Turnover - 10'}}}}
    record={'kind':'completed','key':'k','document':doc,'trace_id':'trace','cost_usd':0.001,
            'prompt':'instructions\n<<<\nTurnover - 10\n>>>',
            'result':{'status':'unresolved','raw':json.dumps(payload),'found_statement':True,
                      'periods':{'current':{'turnover':{'status':'rejected'}}}}}
    traces=[]
    monkeypatch.setattr(B,'restate_trace',lambda *args,**kwargs:traces.append(kwargs))
    completed={'k':record}
    changed=B.revalidate_checkpoint(conn,completed,SimpleNamespace(flush=lambda:None),tmp_path/'checkpoint.jsonl','m')
    assert changed==1 and conn.execute('select turnover from financial_period_summaries').fetchone()[0]==0
    assert completed['k']['cost_usd']==0.001
    assert conn.execute('select cost_usd from vlm_financial_extraction_runs').fetchone()[0]==0
    assert traces[0]['output']['additional_model_cost_usd']==0


def test_saved_duplicate_key_is_safe_only_when_its_values_are_identical():
    assert B.parse_saved_response('{"period_end":"2025-12-31","period_end":"2025-12-31"}') == {'period_end':'2025-12-31'}
    with pytest.raises(ValueError):
        B.parse_saved_response('{"period_end":"2025-12-31","period_end":"2024-12-31"}')
