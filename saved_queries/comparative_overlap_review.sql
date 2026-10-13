-- Every period flagged 'mismatch' during a history backfill: the
-- "previous"-period reading disagreed with what an adjacent filing reported
-- as its own "current" reading for the same period. comparative_overlap_payload
-- holds the two disagreeing values per metric as JSON.
--
-- Triage rule of thumb: turnover/gross_profit/operating_result all moving
-- together by a plausible amount usually means a genuine prior-year
-- restatement (a fact about the company, not a bug). One field alone off by
-- a suspiciously round factor (100x, 1000x -- this has happened on
-- employees) usually means an extraction bug worth reporting.

select
    l.company_name,
    f.company_number,
    f.financial_year,
    f.period_end_on,
    f.comparative_overlap_payload
from financial_period_summaries f
left join leads l on l.company_number = f.company_number
where f.comparative_overlap_status = 'mismatch'
order by f.company_number, f.financial_year;
