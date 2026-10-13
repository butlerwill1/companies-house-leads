-- Companies that look like good PPC leads, with the reasons: size and profit from
-- the filings beside the gap findings (what to pitch) from the web stage. One row
-- per company, the most gaps first. Needs the market and findings steps to have run
-- (`web_population market`, then `web_population findings`): it is empty before.
--
--   gap_segment   greenfield = people search for what it sells but it is not advertising
--                 advertising_poorly = advertising without proper measurement
--   gaps          the plain-English pitch points, one per line
--   already_doing the strengths: what it already does well (call tracking, a CRM...)
--
-- To see only one segment, edit the `gap_segment in (...)` list. The findings are those
-- of the newest findings version; market data is the newest market version.

with fin as (
    select company_number, financial_year, turnover, profit_after_tax,
           row_number() over (partition by company_number order by financial_year desc) as rn
    from company_financial_history
    where turnover is not null
),
gaps as (
    select company_number, group_concat('- ' || detail, char(10)) as gaps
    from company_setup_findings
    where kind = 'gap'
      and finding_version = (select max(finding_version) from company_setup_findings)
    group by company_number
),
strengths as (
    select company_number, group_concat('- ' || detail, char(10)) as already_doing
    from company_setup_findings
    where kind = 'strength'
      and finding_version = (select max(finding_version) from company_setup_findings)
    group by company_number
)
select
    co.company_name,
    m.domain as website,
    m.gap_segment,
    m.setup_level,
    m.gap_count,
    m.advertising_now,
    m.ads_last_shown,
    m.phrase_search_volume as searches_a_month,
    m.weighted_cpc_usd as avg_cpc_usd,
    m.monthly_click_value_usd,
    f.financial_year,
    f.turnover,
    f.profit_after_tax,
    g.gaps,
    s.already_doing
from company_market m
join companies co on co.company_number = m.company_number
left join fin f on f.company_number = m.company_number and f.rn = 1
left join gaps g on g.company_number = m.company_number
left join strengths s on s.company_number = m.company_number
where m.market_version = (select max(market_version) from company_market)
  and m.gap_segment in ('greenfield', 'advertising_poorly')
order by m.gap_count desc, f.turnover desc;
