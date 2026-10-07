-- Multi-year performance for every company with at least N distinct
-- financial years on record (from ch_backfill_history.py), one row per
-- company-year -- browse several companies' trajectories at once instead
-- of pulling up one at a time (see company_financial_history.sql for a
-- single company, in the same shape). Edit the minimum-years threshold
-- below; most companies only have 1 year (see backfill_coverage.sql).

with history_depth as (
    select company_number, count(distinct financial_year) as years_of_history
    from financial_period_summaries
    where period_type = 'current' and financial_year is not null
    group by company_number
    having years_of_history >= 3
)
select
    c.company_name,
    f.company_number,
    d.years_of_history,
    f.financial_year,
    f.turnover,
	f.gross_profit,
	round(100.0 * f.gross_profit / nullif(f.turnover, 0), 1) as gross_margin_pct,
    round(100.0 * (f.turnover - lag(f.turnover) over w) / abs(lag(f.turnover) over w), 1) as turnover_change_pct,
    round(100.0 * f.operating_result / nullif(f.turnover, 0), 1) as operating_margin_pct,
    f.profit_after_tax,
    f.net_assets,
    round(100.0 * (f.net_assets - lag(f.net_assets) over w) / abs(lag(f.net_assets) over w), 1) as net_assets_change_pct,
    f.comparative_overlap_status,
    g.sic_label,
    g.sic_group
from financial_period_summaries f
join history_depth d on d.company_number = f.company_number
left join companies c on c.company_number = f.company_number
left join leads l on l.company_number = f.company_number
left join sic_groups g on g.sic_code = substr(l.sic_1, 1, 5)
where f.period_type = 'current'
window w as (partition by f.company_number order by f.financial_year)
order by f.company_number, f.financial_year;
