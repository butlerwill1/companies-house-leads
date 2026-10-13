"""Run a bounded, resumable XHTML recovery batch for latest screened-company gaps.

This is the production command for a specific approved recovery run. ``plan``
reads SQLite and freezes an exact list of filings whose newest canonical year
lacks turnover or profit after tax, among companies in company_search_screen.
It does not select older years simply because they have better figures.

``run`` downloads only those filings (or reads their local cache), calls the
existing XHTML text-recovery prompt through OpenRouter, validates quoted figures,
and fills only empty turnover/profit cells in the selected accounting period.
Comparatives and other financial fields are excluded. It never repairs an
already populated value or estimates figures absent from the filing.

Each API request reserves its maximum token cost before sending, with bounded
output and provider price ceilings. There are no automatic paid retries. The
required --max-cost-usd applies across all resumptions in the run directory.
Interrupted requests with unknown billing retain their full reservation and
are not repeated automatically. A completed record is fsynced to checkpoint.jsonl
before the database write, allowing a restart to replay it without another call.

Every case has a Langfuse trace on the existing localhost:3000 instance, using
the config's key_env; paid calls include their exact prompt, response and usage.
manifest.json fixes scope; summary.json records outcomes and spend. These files,
cached filings and readable Markdown copies are local, gitignored working data.
Deleting the checkpoint removes resume protection and can cause fresh spend.

``revalidate`` applies the current deterministic validator to saved responses,
using the exact original prompt text. It can fill newly accepted missing cells
in the frozen period, but never sends model requests. Original paid costs stay
on the checkpoint; additional validation audit rows have zero cost. A duplicate
JSON key is accepted on replay only when its values are identical; conflicting
duplicates remain errors. Literal financial-table dashes can validate as zero.

Usage (run requires authorization for this specific batch and budget):
    python -m scripts.pdf_vision_extraction.xhtml_recovery_batch plan --run-dir logs/xhtml-recovery/latest-screened-2026-10-12
    python -m scripts.pdf_vision_extraction.xhtml_recovery_batch run --run-dir logs/xhtml-recovery/latest-screened-2026-10-12 --max-cost-usd 3
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any

from dotenv import load_dotenv
import requests
import yaml

from companies_house_core.companies_house_extractor import filed_report_text
from scripts.business_profile_classifier.save_raw_filings import to_readable_markdown
from scripts.pdf_vision_extraction import xhtml_text_financials as tf
from scripts.langfuse_eval_helpers.langfuse_tracing import case_trace, flush, langfuse_from_config, observation, restate_trace

CONFIG = Path('evals/xhtml_financial_recovery_configs/default.yaml')
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'


def latest_screened_gaps(conn: sqlite3.Connection) -> list[dict]:
    """Exact newest-period gaps, one filing per company, regardless of screen answer."""
    return [dict(r) for r in conn.execute('''
        with ranked as (
            select h.*, row_number() over(partition by company_number order by financial_year desc) rn
            from company_financial_history h
        )
        select r.company_number, co.company_name, r.document_id, r.period_type,
               r.financial_year, r.period_end_on, r.turnover, r.profit_after_tax, f.id as financial_summary_id
        from ranked r join documents d using(document_id)
        join companies co using(company_number)
        join financial_period_summaries f on f.company_number=r.company_number
             and f.document_id=r.document_id and f.period_type=r.period_type
        where rn=1 and (r.turnover is null or r.profit_after_tax is null)
          and coalesce(d.xhtml_url,'')!=''
          and exists(select 1 from company_search_screen s where s.company_number=r.company_number)
        order by r.company_number
    ''')]


def limit_to_selected_period(result: dict, doc: dict) -> dict:
    """Discard comparative recovery and reject an explicitly mismatched period date."""
    period = doc['period_type']
    entry = dict((result.get('periods') or {}).get(period) or {})
    expected, actual = doc.get('period_end_on'), entry.get('period_end')
    problem = result.get('problem')
    if expected and actual and expected != actual:
        problem = 'model period end differs from selected accounting period'
        for metric in tf.METRICS:
            entry[metric] = {'status':'rejected','reason':problem}
    status = result['status']
    if status == 'recovered' and not any((entry.get(m) or {}).get('status')=='recovered' for m in tf.METRICS):
        status = 'unresolved'
    return {**result,'status':status,'problem':problem,'periods':{period:entry}}


def append_record(path: Path, record: dict) -> None:
    """Persist a case or spend reservation before progressing to the next operation."""
    with path.open('a',encoding='utf-8') as handle:
        handle.write(json.dumps(record,ensure_ascii=False)+'\n')
        handle.flush()
        os.fsync(handle.fileno())


def resume_state(records: list[dict]) -> tuple[dict, set, float]:
    completed = {r['key']:r for r in records if r['kind']=='completed'}
    started = {r['key']:r for r in records if r['kind']=='request_started'}
    pending = set(started)-set(completed)
    spent = sum(r['cost_usd'] for r in completed.values()) + sum(started[k]['reserved_usd'] for k in pending)
    return completed,pending,spent


class BudgetExhausted(RuntimeError):
    pass


class SpendBudget:
    """Account for completed charges and outstanding maximum-cost reservations."""
    def __init__(self, cap: float, spent: float = 0):
        self.cap,self.spent=cap,spent

    def reserve(self, amount: float) -> None:
        if self.spent+amount > self.cap:
            raise BudgetExhausted('next request could exceed the total run cap')
        self.spent += amount


class BoundedCall:
    """One HTTP attempt; reserve byte-bound input and limited output at price ceilings."""
    def __init__(self, api_key: str, config: dict, budget: SpendBudget, journal, send=requests.post):
        self.api_key,self.config,self.budget,self.journal,self.send=api_key,config,budget,journal,send
        self.cost=0.0
        self.called=False

    def __call__(self, prompt: str):
        cfg=self.config
        input_rate=cfg['input_price_per_million']/1_000_000
        output_rate=cfg['output_price_per_million']/1_000_000
        # UTF-8 bytes upper-bound text tokens for this byte-level tokenizer;
        # 1,024 extra tokens conservatively cover chat serialization overhead.
        reserved=(len(prompt.encode('utf-8'))+1024)*input_rate+cfg['max_output_tokens']*output_rate
        self.budget.reserve(reserved)
        self.journal({'kind':'request_started','reserved_usd':reserved,'prompt':prompt})
        self.cost,self.called=reserved,True
        response=self.send(url=OPENROUTER_URL,timeout=180,
            headers={'Authorization':f'Bearer {self.api_key}','Content-Type':'application/json'},
            json={'model':cfg['model'],'messages':[{'role':'user','content':prompt}],
                  'temperature':0,'response_format':{'type':'json_object'},
                  'max_tokens':cfg['max_output_tokens'],'reasoning':{'effort':'low'},
                  'provider':{'max_price':{'prompt':cfg['input_price_per_million'],
                                           'completion':cfg['output_price_per_million']}}})
        response.raise_for_status()
        body=response.json()
        if body.get('error'): raise RuntimeError('OpenRouter returned a model error')
        usage=body.get('usage') or {}
        raw=body['choices'][0]['message']['content'] or ''
        if 'prompt_tokens' in usage and 'completion_tokens' in usage:
            estimated=usage['prompt_tokens']*input_rate+usage['completion_tokens']*output_rate
            actual=max(estimated,float(usage.get('cost') or 0))
            self.budget.spent += actual-reserved
            self.cost=actual
        usage={**usage,'generation_id':body.get('id')}
        return raw,usage


def apply_completed(conn: sqlite3.Connection, record: dict, model: str) -> dict:
    """Replay a saved validated result once, with fill-only storage and an audit row."""
    doc=record['document']
    if conn.execute("select 1 from vlm_financial_extraction_runs where document_id=? and vision_model=? "
                    "and locator_model=?",(doc['document_id'],tf.STAGE_MODEL,tf.PROMPT_VERSION)).fetchone():
        return {'turnover':0,'profit_after_tax':0}
    return tf.store_recovery(conn,doc['company_number'],doc['document_id'],record['result'],model,record['cost_usd'])


def coverage_summary(conn: sqlite3.Connection, docs: list[dict]) -> dict:
    """Measure real cell changes against the frozen baseline, including resumed work."""
    out={'companies_improved':0,'turnover_cells_filled':0,'profit_cells_filled':0,
         'with_both_before':0,'with_both_after':0}
    for doc in docs:
        row=conn.execute('select turnover,profit_after_tax from financial_period_summaries where id=?',
                         (doc['financial_summary_id'],)).fetchone()
        changed=False
        for metric,key in [('turnover','turnover_cells_filled'),('profit_after_tax','profit_cells_filled')]:
            if doc[metric] is None and row[metric] is not None:
                out[key]+=1
                changed=True
        out['companies_improved']+=changed
        out['with_both_before']+=all(doc[m] is not None for m in tf.METRICS)
        out['with_both_after']+=all(row[m] is not None for m in tf.METRICS)
    return out


def parse_saved_response(raw: str) -> dict:
    """Parse saved JSON; allow identical repeated keys, never conflicting values."""
    try:
        return tf.parse_json_response(raw)
    except ValueError:
        def pairs(items):
            result={}
            for key,value in items:
                if key in result and json.dumps(result[key],sort_keys=True)!=json.dumps(value,sort_keys=True):
                    raise ValueError('conflicting duplicate JSON key')
                result[key]=value
            return result
        result=json.loads(raw,object_pairs_hook=pairs)
        if not isinstance(result,dict): raise ValueError('response must be a JSON object')
        return result


def revalidate_checkpoint(conn, completed: dict, client, checkpoint: Path, model: str) -> int:
    """Free validation replay; only newly accepted, still-empty selected cells can change."""
    changed=0
    for key,record in list(completed.items()):
        raw=record['result'].get('raw')
        prompt=record.get('prompt')
        if not raw or not prompt: continue
        text=prompt.split('<<<\n',1)[1].rsplit('\n>>>',1)[0]
        try:
            validated=tf.validate_recovery(parse_saved_response(raw),text,record['result']['found_statement'])
        except (ValueError,TypeError,KeyError):
            continue
        result=limit_to_selected_period({**record['result'],**validated,'problem':None},record['document'])
        period=record['document']['period_type']
        before=record['result']['periods'].get(period) or {}
        after=result['periods'].get(period) or {}
        if not any((before.get(m) or {}).get('status')!='recovered'
                   and (after.get(m) or {}).get('status')=='recovered' for m in tf.METRICS):
            continue
        audit_result={**result,'usage':{},'problem':'free validation replay of the saved model response'}
        cells=tf.store_recovery(conn,record['document']['company_number'],record['document']['document_id'],
                                audit_result,model,0.0)
        replay={**record,'result':result,'validation_replay':True,'cells_filled':cells}
        append_record(checkpoint,replay)
        completed[key]=replay
        restate_trace(client,record['trace_id'],f"xhtml-recovery/{record['document']['company_number']}",
                      metadata={'validation_replay':True},
                      input={'document':record['document'],'prompt':prompt},
                      output={'status':result['status'],'validation_replay':True,'cells_filled':cells,
                              'cost_usd':record['cost_usd'],'additional_model_cost_usd':0})
        flush(client)
        changed+=sum(cells.values())
    return changed


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'): stream.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command',choices=['plan','run','revalidate'])
    parser.add_argument('--db',type=Path,default=Path('companies-house.db'))
    parser.add_argument('--config',type=Path,default=CONFIG)
    parser.add_argument('--run-dir',type=Path,required=True)
    parser.add_argument('--max-cost-usd',type=float)
    parser.add_argument('--limit',type=int,help='Process at most this many unfinished manifest cases.')
    args=parser.parse_args(argv)
    args.run_dir.mkdir(parents=True,exist_ok=True)
    manifest_path=args.run_dir/'manifest.json'
    if args.command=='plan':
        if manifest_path.exists(): parser.error('manifest already exists; reuse it to preserve the approved scope')
        with sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True) as conn:
            conn.row_factory=sqlite3.Row
            docs=latest_screened_gaps(conn)
        manifest_path.write_text(json.dumps({'created_at':datetime.now(timezone.utc).isoformat(),
                                             'documents':docs},indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'selected_filings':len(docs),'manifest':str(manifest_path)}))
        return 0
    if args.command=='run' and (args.max_cost_usd is None or args.max_cost_usd<=0):
        parser.error('run requires a positive --max-cost-usd')
    load_dotenv('.env')
    cfg=yaml.safe_load(args.config.read_text(encoding='utf-8'))
    if cfg['langfuse'].get('host')!='http://localhost:3000': parser.error('use the existing local Langfuse instance')
    lf=langfuse_from_config(cfg)
    if lf is None or not lf.auth_check(): parser.error('configured local Langfuse is unavailable; no paid calls sent')
    dataset=cfg['langfuse']['dataset']
    lf.create_dataset(name=dataset,description='Production XHTML financial recovery with quoted source evidence.')
    docs=json.loads(manifest_path.read_text(encoding='utf-8'))['documents']
    checkpoint=args.run_dir/'checkpoint.jsonl'
    records=[json.loads(s) for s in checkpoint.read_text(encoding='utf-8').splitlines()] if checkpoint.exists() else []
    completed,pending,spent=resume_state(records)
    cap=args.max_cost_usd
    if args.command=='revalidate':
        cap=json.loads((args.run_dir/'summary.json').read_text(encoding='utf-8'))['cap_usd']
    budget=SpendBudget(cap,spent)
    conn=sqlite3.connect(args.db,timeout=120)
    conn.row_factory=sqlite3.Row
    for r in completed.values(): apply_completed(conn,r,cfg['model'])
    if args.command=='revalidate':
        cells=revalidate_checkpoint(conn,completed,lf,checkpoint,cfg['model'])
        print(json.dumps({'revalidated_cells_filled':cells,'additional_model_cost_usd':0}),flush=True)
    processed=0
    for doc in (docs if args.command=='run' else []):
        key=f"{cfg['model']}:{tf.PROMPT_VERSION}:{doc['company_number']}:{doc['document_id']}:{doc['period_type']}"
        if key in completed or key in pending: continue
        if args.limit is not None and processed>=args.limit: break
        record={'kind':'completed','key':key,'document':doc,'model':cfg['model'],'cost_usd':0.0}
        xhtml=tf.fetch_xhtml(doc['document_id'],os.environ['COMPANIES_HOUSE_API_KEY'],conn,doc['company_number'])
        if xhtml:
            readable=args.run_dir/'filings'
            readable.mkdir(exist_ok=True)
            (readable/f"{doc['company_number']}-{doc['document_id']}.md").write_text(
                to_readable_markdown(xhtml),encoding='utf-8')
        text,found=tf.statement_windows(filed_report_text(xhtml)) if xhtml else ('',False)
        prompt=tf.build_prompt(doc['company_name'],text) if text.strip() else None
        result={'status':'unresolved','problem':'no statement text found' if xhtml else 'filing download failed',
                'periods':{},'usage':{},'raw':None,'prompt_version':tf.PROMPT_VERSION,'found_statement':found}
        item=lf.create_dataset_item(dataset_name=dataset,id=doc['document_id'],input=doc)
        with case_trace(lf,name=f"xhtml-recovery/{doc['company_number']}",tags=['xhtml-financial-recovery'],
                        session_id=args.run_dir.name,metadata={'dataset':dataset,'dataset_item_id':item.id},
                        input={'document':doc,'prompt':prompt}) as root:
            record['trace_id']=root.trace_id
            caller=BoundedCall(os.environ['OPENROUTER_API_KEY'],cfg,budget,
                               lambda event: append_record(checkpoint,{**event,'key':key}))
            if prompt:
                with observation(root,name='xhtml-text-recovery',as_type='generation',model=cfg['model'],input=prompt) as gen:
                    try:
                        raw,usage=caller(prompt)
                        result.update(raw=raw,usage=usage)
                        result.update(problem=None,**tf.validate_recovery(tf.parse_json_response(raw),text,found))
                    except BudgetExhausted:
                        gen.update(output={'problem':'budget exhausted'})
                        root.update(output={'problem':'budget exhausted'})
                        flush(lf)
                        break
                    except Exception as exc:
                        result['problem']=f'{type(exc).__name__}: request or response failed'
                    gen.update(output=result['raw'],usage_details={
                        'input':int(result['usage'].get('prompt_tokens') or 0),
                        'output':int(result['usage'].get('completion_tokens') or 0)},
                        cost_details={'total':caller.cost})
            record.update(result=limit_to_selected_period(result,doc),cost_usd=caller.cost,prompt=prompt)
            append_record(checkpoint,record)
            record['cells_filled']=apply_completed(conn,record,cfg['model'])
            completed[key]=record
            root.update(output={'status':record['result']['status'],'cells_filled':record['cells_filled'],
                                'cost_usd':record['cost_usd']})
        flush(lf)
        processed+=1
        print(json.dumps({'finished':len(completed),'of':len(docs),'company_number':doc['company_number'],
                          'status':record['result']['status'],'cells_filled':record['cells_filled'],
                          'cost_usd':round(budget.spent,6),'trace_id':record['trace_id']}),flush=True)
    summary={'selected':len(docs),'completed':len(completed),'pending_unknown_billing':len(pending),
             'cost_usd':round(budget.spent,6),'cap_usd':budget.cap,
             'status':dict(Counter(r['result']['status'] for r in completed.values())),
             **coverage_summary(conn,docs)}
    (args.run_dir/'summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary))
    conn.close()
    flush(lf)
    return 0 if len(completed)==len(docs) else 1


if __name__=='__main__':
    raise SystemExit(main())
