-- Companies with both turnover and profit data on their current period
-- whose turnover falls in the range below. Mirrors the selection logic in
-- select_turnover_band_sample() in scripts/enrichment/ch_backfill_history.py
-- -- run this first to see how large a cohort a --turnover-band/--sample
-- backfill would draw from. Edit the range before running.

select
    c.company_name,
    f.company_number,
    l.account_category,
    g.sic_label,
    f.turnover,
    f.profit_after_tax
from financial_period_summaries f
left join companies c on c.company_number = f.company_number
left join leads l on l.company_number = f.company_number
left join sic_groups g on g.sic_code = substr(l.sic_1, 1, 5)
where f.period_type = 'current'
  and f.turnover between 5000000 and 20000000
  and f.profit_after_tax is not null
order by f.turnover desc;
