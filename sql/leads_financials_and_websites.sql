-- The whole picture of each company on one row: its financials, what the
-- business does, its website and the marketing tools on it, and (once the market
-- step has run) whether it advertises and how big its search demand is.
-- This is the query to browse leads with.
--
-- Joins, newest version of each:
--   financials   company_financial_history (latest year with a turnover, and the year before)
--   screen       company_search_screen (the search screen's answer: likely / possible / unlikely)
--   website      company_web_identity_current (the chosen site, newest resolver version) + web_sites (tools found on it)
--   what it does company_google_listing (Google's category, rating, reviews) +
--                company_web_profile (the model's summary, customer type, how customers convert)
--   market       company_market (advertising now, search demand, gap segment), empty until
--                `web_population market` and `findings` have run
--
-- Only companies with a chosen website are listed. To list every company, change
-- `join ident` to `left join ident`.
--
-- gap_segment: greenfield (search demand, not advertising) / advertising_poorly /
-- advertising_well / low_demand / site_first / unknown (docs/WEB_STAGE_PLAN.md).

with latest_fin as (
    select company_number, financial_year, turnover, profit_after_tax, employees,
           row_number() over (partition by company_number order by financial_year desc) as rn
    from company_financial_history
    where turnover is not null
),
fin as (
    select l.company_number, l.financial_year, l.turnover, l.profit_after_tax, l.employees,
           p.turnover as previous_turnover
    from latest_fin l
    left join company_financial_history p
           on p.company_number = l.company_number and p.financial_year = l.financial_year - 1
    where l.rn = 1
),
ident as (
    select company_number, domain, tier,
           row_number() over (partition by company_number order by id desc) as rn
    from company_web_identity_current
    where role = 'main' and domain is not null
),
listing as (
    select company_number, category, rating, rating_count,
           row_number() over (partition by company_number order by id desc) as rn
    from company_google_listing
),
profile as (
    select company_number, summary, customer_type, conversion_action, geography, main_town,
           row_number() over (partition by company_number order by profiled_at desc) as rn
    from company_web_profile
),
market as (
    select * from company_market
    where market_version = (select max(market_version) from company_market)
)
select
    co.company_number,
    co.company_name,
    sg.sic_label,
    f.financial_year,
    f.turnover,
    round(100.0 * (f.turnover - f.previous_turnover) / nullif(f.previous_turnover, 0), 1) as turnover_change_pct,
    f.profit_after_tax,
    round(100.0 * f.profit_after_tax / nullif(f.turnover, 0), 1) as net_margin_pct,
    f.employees,
    sc.answer as search_screen,
    g.category as google_category,
    g.rating,
    g.rating_count as reviews,
    pr.summary,
    pr.customer_type,
    pr.conversion_action,
    trim(coalesce(pr.geography, '') || ' ' || coalesce(pr.main_town, '')) as area_served,
    i.domain as website,
    i.tier as website_match,
    ws.crawl_status,
    ws.has_google_ads_tag,
    ws.has_ads_conversion_event,
    ws.crm_vendors,
    ws.call_tracking_vendor,
    ws.social_pixels,
    ws.agency_credit,
    m.advertising_now,
    m.ads_last_shown,
    m.phrase_search_volume,
    m.weighted_cpc_usd,
    m.gap_segment,
    m.setup_level,
    m.gap_count
from companies co
join fin f on f.company_number = co.company_number
join ident i on i.company_number = co.company_number and i.rn = 1
left join sic_groups sg on sg.sic_code = co.sic_code_primary
left join company_search_screen sc
       on sc.company_number = co.company_number
      and sc.prompt_version = (select max(prompt_version) from company_search_screen)
left join listing g on g.company_number = co.company_number and g.rn = 1
left join profile pr on pr.company_number = co.company_number and pr.rn = 1
left join web_sites ws
       on ws.domain = i.domain
      and ws.crawl_version = (select max(crawl_version) from web_sites)
      and ws.rule_version = (select max(rule_version) from web_sites)
left join market m on m.company_number = co.company_number
order by f.turnover desc;
