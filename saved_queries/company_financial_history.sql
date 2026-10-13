-- One company's full multi-year trajectory: raw figures, year-over-year
-- percentage change, gross/operating margins, and whether each period was
-- cross-checked against an adjacent filing (see comparative_overlap_review.sql
-- for what a "mismatch" means). A single-year snapshot of a lumpy or
-- declining company can be actively misleading -- this is the query to run
-- before trusting one.
--
-- Edit the company number below before running.

select
    financial_year,
    period_end_on,
    turnover,
    round(100.0 * (turnover - lag(turnover) over w) / abs(lag(turnover) over w), 1) as turnover_change_pct,
    gross_profit,
    round(100.0 * gross_profit / nullif(turnover, 0), 1) as gross_margin_pct,
    operating_result,
    round(100.0 * operating_result / nullif(turnover, 0), 1) as operating_margin_pct,
    profit_after_tax,
    cash,
    net_assets,
    round(100.0 * (net_assets - lag(net_assets) over w) / abs(lag(net_assets) over w), 1) as net_assets_change_pct,
    employees,
    data_source,
    comparative_overlap_status
from financial_period_summaries
where company_number = '01407612'
  and period_type = 'current'
window w as (order by financial_year)
order by financial_year;
