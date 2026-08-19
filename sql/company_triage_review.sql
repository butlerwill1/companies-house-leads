-- Gate A triage results (from scripts/analysis/ch_company_triage.py),
-- pivoted from the company_signals EAV table into one row per company.
--
-- trading_status is evidence, not a verdict. In particular "holding" means
-- the legal entity employs nobody directly -- it does NOT mean there is no
-- business: several group parents here consolidate a real trading operation
-- (RICHARDSONS (HOLDINGS) files as a car dealership with two East Yorkshire
-- sites). Only the filed narrative separates a true investment vehicle from
-- a trading group filing through its parent.
--
-- Filter the where clause to the bucket you want to review.

with latest_financials as (
    -- One row per company: multi-year history would otherwise fan the join
    -- out and pick an arbitrary year's turnover.
    select company_number, turnover, employees
    from financial_period_summaries f
    where period_type = 'current'
      and id = (
          select id from financial_period_summaries
          where company_number = f.company_number and period_type = 'current'
          order by financial_year desc, id desc limit 1
      )
)
select
    c.company_number,
    c.company_name,
    c.sic_code_primary,
    g.sic_label,
    max(case when s.signal_key = 'trading_status' then s.signal_text end) as trading_status,
    max(case when s.signal_key = 'trading_status_reason' then s.signal_text end) as reason,
    max(case when s.signal_key = 'duplicate_of' then s.signal_text end) as duplicate_of,
    max(case when s.signal_key = 'revenue_per_employee' then s.signal_int end) as revenue_per_employee,
    max(case when s.signal_key = 'turnover_without_employees' then s.signal_bool end) as turnover_without_employees,
    max(case when s.signal_key = 'sic_is_catch_all' then s.signal_bool end) as sic_is_catch_all,
    max(case when s.signal_key = 'gross_margin_pct' then s.signal_real end) as gross_margin_pct,
    f.turnover,
    f.employees
from companies c
join company_signals s on s.company_number = c.company_number
left join sic_groups g on g.sic_code = c.sic_code_primary
left join latest_financials f on f.company_number = c.company_number
group by c.company_number
having trading_status <> 'trading'
order by f.turnover desc nulls last;
