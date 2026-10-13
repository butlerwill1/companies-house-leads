-- Current extraction and lead funnel, one row per stage. Read-only SQLite.
-- Open in DB Browser for SQLite's Execute SQL tab against companies-house.db.
--
-- Counts are DISTINCT COMPANIES, not filings, model runs or websites.
-- Sections contain both funnel stages and side branches; read the denominator
-- and notes rather than treating every row as a step in one shrinking funnel.
--
-- Financial definitions:
--   usable = turnover AND profit_after_tax in the SAME stored period;
--   all seven = those two plus gross_profit, operating_result, cash,
--               net_assets and employees, all in the SAME period.
-- Zero counts as present. 'Latest' means the newest financial_year in the
-- canonical company_financial_history view, not the newest year with turnover.
-- Stored summary rows can contain no extracted metrics at all.
--
-- The latest screen/profile/site summary per entity is selected by timestamp,
-- with deterministic tie breaks. Identity uses company_web_identity_current.
-- The web queue matches web_rank_order.build_queue's current membership:
-- screen passes minus Gate A duplicate_of signals. It does not read the frozen
-- logs/web/queue-order.json. SQL cannot count pilot results saved only in logs.
with
financial_coverage as (
    select company_number,
           max(turnover is not null or gross_profit is not null
               or operating_result is not null or profit_after_tax is not null
               or cash is not null or net_assets is not null
               or employees is not null) as has_any_metric,
           max(turnover is not null) as has_turnover,
           max(turnover is not null and profit_after_tax is not null) as usable,
           max(turnover is not null and profit_after_tax is not null
               and gross_profit is not null and operating_result is not null
               and cash is not null and net_assets is not null
               and employees is not null) as all_seven
    from financial_period_summaries
    group by company_number
),
financial_ranked as (
    select h.*,
           row_number() over (
               partition by company_number order by financial_year desc
           ) as rn
    from company_financial_history h
),
latest_financial as (
    select *,
           turnover is not null and profit_after_tax is not null as usable,
           turnover is not null and profit_after_tax is not null
               and gross_profit is not null and operating_result is not null
               and cash is not null and net_assets is not null
               and employees is not null as all_seven
    from financial_ranked where rn = 1
),
screen_ranked as (
    select s.*,
           row_number() over (
               partition by company_number order by screened_at desc, id desc
           ) as rn
    from company_search_screen s
),
screen as (select * from screen_ranked where rn = 1),
web_queue as (
    select s.company_number
    from screen s
    where s.passes = 1
      and not exists (
          select 1 from company_signals d
          where d.company_number = s.company_number
            and d.signal_key = 'duplicate_of'
            and coalesce(d.signal_text, '') != ''
      )
),
identity as (
    select company_number,
           max(role = 'main' and domain is not null) as chosen,
           max(role = 'main' and domain is not null and tier = 'verified') as verified,
           max(role = 'main' and domain is not null and tier = 'probable') as probable,
           max(tier = 'ambiguous') as ambiguous
    from company_web_identity_current group by company_number
),
chosen_ranked as (
    select i.*,
           row_number() over (partition by company_number order by id desc) as rn
    from company_web_identity_current i
    where role = 'main' and domain is not null
),
chosen as (select * from chosen_ranked where rn = 1),
profile_ranked as (
    select p.*,
           row_number() over (
               partition by company_number
               order by profiled_at desc, profile_version desc, model desc
           ) as rn
    from company_web_profile p
),
profile as (select * from profile_ranked where rn = 1),
site_ranked as (
    select w.*,
           row_number() over (
               partition by domain
               order by summarised_at desc, crawl_version desc, rule_version desc
           ) as rn
    from web_sites w
),
site as (select * from site_ranked where rn = 1),
queue_status as (
    select q.company_number,
           i.company_number is not null as looked_up,
           coalesce(i.chosen, 0) as has_site,
           coalesce(i.verified, 0) as verified,
           coalesce(i.probable, 0) as probable,
           i.chosen = 0 and i.ambiguous = 1 as ambiguous_only,
           i.chosen = 0 and i.ambiguous = 0 as no_site_found,
           ch.domain,
           ws.crawl_status,
           ws.has_google_ads_tag,
           ws.has_ads_conversion_event,
           p.company_number is not null as profile_attempted,
           p.company_number is not null and coalesce(p.problem, '') = '' as profile_clean,
           p.channel_fit,
           coalesce(f.usable, 0) as latest_financial_usable
    from web_queue q
    left join identity i using (company_number)
    left join chosen ch using (company_number)
    left join site ws on ws.domain = ch.domain
    left join profile p using (company_number)
    left join latest_financial f using (company_number)
),
totals as (
    select
        (select count(*) from leads) as bulk,
        (select count(*) from financial_coverage) as financial,
        (select count(*) from screen) as screened,
        (select count(*) from screen where passes = 1) as passed,
        (select count(*) from web_queue) as queued,
        (select count(*) from queue_status where looked_up = 1) as looked_up,
        (select count(*) from queue_status where has_site = 1) as websites,
        (select count(*) from queue_status where profile_attempted = 1 and has_site = 1) as profiled,
        (select count(*) from queue_status where profile_clean = 1 and has_site = 1) as clean
),
stages (sort_order, section, stage, companies, denominator, notes) as (
    select 1, 'Ingestion', 'Bulk-filtered leads', bulk, bulk,
           'Starting list; not all enriched.' from totals
    union all select 2, 'Ingestion', 'Enrichment attempted', count(*),  (select bulk from totals),
           'Lead status is not pending.' from leads where status != 'pending'
    union all select 3, 'Ingestion', 'XHTML enrichment done', count(*),  (select bulk from totals),
           'Lead status done; does not mean every financial metric was extracted.'
           from leads where status = 'done'
    union all select 4, 'Ingestion', 'No XHTML recorded', count(*),  (select bulk from totals),
           'Lead status no_xhtml; PDF recovery is a separate path.'
           from leads where status = 'no_xhtml'
    union all select 5, 'Ingestion', 'Enrichment errors', count(*),  (select bulk from totals),
           'Lead status error.' from leads where status = 'error'
    union all select 6, 'Ingestion', 'Still pending', count(*),  (select bulk from totals),
           'No completed enrichment attempt.' from leads where status = 'pending'
    union all select 7, 'Ingestion', 'Company API profiles stored', count(*),  (select bulk from totals),
           'The companies table; differs from financial coverage when PDF-only entities were imported.'
           from companies
    union all select 10, 'Financial coverage', 'Companies with stored accounts rows', financial, financial,
           'Includes accounts rows with no populated metrics and PDF imports without an API profile.' from totals
    union all select 11, 'Financial coverage', 'Any extracted metric, any period', count(*),  (select financial from totals),
           'At least one of the seven fields is present.'
           from financial_coverage where has_any_metric = 1
    union all select 12, 'Financial coverage', 'Turnover available, any period', count(*),  (select financial from totals),
           'May come from an older accounting period.'
           from financial_coverage where has_turnover = 1
    union all select 13, 'Financial coverage', 'Usable turnover and profit, any period', count(*),  (select financial from totals),
           'Both in the same period; the financial requirement for the search screen.'
           from financial_coverage where usable = 1
    union all select 14, 'Financial coverage', 'All seven fields, any period', count(*),  (select financial from totals),
           'Strict completeness; all seven present together, possibly in an older year.'
           from financial_coverage where all_seven = 1
    union all select 15, 'Financial coverage', 'Usable turnover and profit, latest year', count(*),  (select financial from totals),
           'Newest recorded financial year, even if it lacks turnover.'
           from latest_financial where usable = 1
    union all select 16, 'Financial coverage', 'All seven fields, latest year', count(*),  (select financial from totals),
           'Strict completeness in the newest recorded financial year.'
           from latest_financial where all_seven = 1
    union all select 20, 'Search screen', 'Companies screened', screened, t.n,
           'Screen population also needed a filed principal-activity narrative.'
           from totals cross join (select count(*) n from financial_coverage where usable = 1) t
    union all select 21, 'Search screen', 'Likely search/PPC prospects', count(*),  (select screened from totals),
           'Potential customers likely search online and buy, book or enquire; not observed advertising.'
           from screen where answer = 'likely'
    union all select 22, 'Search screen', 'Possible prospects', count(*),  (select screened from totals),
           'Kept for further investigation.' from screen where answer = 'possible'
    union all select 23, 'Search screen', 'Unlikely / rejected', count(*),  (select screened from totals),
           'Rejected by the filing screen.' from screen where passes = 0
    union all select 24, 'Search screen', 'Passed with a processing problem', count(*),  (select screened from totals),
           'Fails open; includes null or unparseable answers.'
           from screen
           where passes = 1 and (answer is null or answer not in ('likely', 'possible') or coalesce(problem, '') != '')
    union all select 25, 'Search screen', 'All screen passes', passed, screened,
           'Likely + possible + failed-open results.' from totals
    union all select 26, 'Search screen', 'Web queue after duplicate removal', queued, passed,
           'Current screen passes excluding non-empty duplicate_of signals.' from totals
    union all select 30, 'Website funnel', 'Website discovery attempted', looked_up, queued,
           'Has an identity result, including ambiguous and none.' from totals
    union all select 31, 'Website funnel', 'Still awaiting website discovery', queued - looked_up, queued,
           'No identity result in the queue.' from totals
    union all select 32, 'Website funnel', 'Chosen website', websites, looked_up,
           'Main site selected, rather than merely a candidate.' from totals
    union all select 33, 'Website funnel', 'Verified main website', count(*),  (select looked_up from totals),
           'Registered company number was matched.' from queue_status where verified = 1
    union all select 34, 'Website funnel', 'Probable main website', count(*),  (select looked_up from totals),
           'Accepted identity evidence without a registered-number match.'
           from queue_status where probable = 1
    union all select 35, 'Website funnel', 'Ambiguous only', count(*),  (select looked_up from totals),
           'Candidate found, but no main website selected.'
           from queue_status where ambiguous_only = 1
    union all select 36, 'Website funnel', 'No website found', count(*),  (select looked_up from totals),
           'Recorded lookup result; does not prove no website exists.'
           from queue_status where no_site_found = 1
    union all select 37, 'Website funnel', 'Google Maps listing recorded', count(*),  (select queued from totals),
           'At least one stored listing for a queue company.'
           from web_queue q
           where exists (select 1 from company_google_listing g where g.company_number = q.company_number)
    union all select 38, 'Website funnel', 'Chosen website has crawl summary', count(*),  (select websites from totals),
           'Companies, not distinct domains; a crawl record may be blocked or thin.'
           from queue_status where has_site = 1 and crawl_status is not null
    union all select 39, 'Website funnel', 'Chosen website crawl OK', count(*),  (select websites from totals),
           'Readable crawl.' from queue_status where has_site = 1 and crawl_status = 'ok'
    union all select 40, 'Website funnel', 'Chosen website crawl thin', count(*),  (select websites from totals),
           'Limited readable content.' from queue_status where has_site = 1 and crawl_status = 'thin'
    union all select 41, 'Website funnel', 'Chosen website crawl blocked', count(*),  (select websites from totals),
           'Blocked content.' from queue_status where has_site = 1 and crawl_status = 'blocked'
    union all select 42, 'Website funnel', 'Website classifier attempted', profiled, websites,
           'Latest stored company profile; includes reported problems.' from totals
    union all select 43, 'Website funnel', 'Classifier without reported problem', clean, profiled,
           'Processing/evidence checks passed; does not establish classification accuracy.' from totals
    union all select 44, 'Website funnel', 'Classifier has reported problem', profiled - clean, profiled,
           'For example no site text or a quote-validation failure.' from totals
    union all select 45, 'Website funnel', 'Clean profile + latest usable financials', count(*),  (select clean from totals),
           'Chosen site, no profile problem, and latest-year turnover and profit together.'
           from queue_status
           where has_site = 1 and profile_clean = 1 and latest_financial_usable = 1
    union all select 46, 'Website funnel', 'Clean profile says search or both channels', count(*),  (select clean from totals),
           'Model channel fit; separate from confirmed PPC activity and sales readiness.'
           from queue_status
           where has_site = 1 and profile_clean = 1 and channel_fit in ('search', 'both')
    union all select 47, 'Website funnel', 'Google Ads tag detected', count(*),  (select websites from totals),
           'Technology present; not proof of active campaigns.'
           from queue_status where has_site = 1 and has_google_ads_tag = 1
    union all select 48, 'Website funnel', 'Ads conversion event detected', count(*),  (select websites from totals),
           'Detected setup, not a validated measurement audit.'
           from queue_status where has_site = 1 and has_ads_conversion_event = 1
    union all select 50, 'Downstream', 'Market assessment stored', count(*),  (select queued from totals),
           'SQL sees persisted company_market rows; pilot JSON files are excluded.'
           from web_queue q
           where exists (select 1 from company_market m where m.company_number = q.company_number)
    union all select 51, 'Downstream', 'Setup findings stored', count(*),  (select queued from totals),
           'At least one gap or strength finding.' from web_queue q
           where exists (select 1 from company_setup_findings f where f.company_number = q.company_number)
    union all select 52, 'Downstream', 'Handover / outcomes recorded', count(*),  (select queued from totals),
           'At least one lead_outcomes row; not necessarily contacted.'
           from web_queue q
           where exists (select 1 from lead_outcomes o where o.company_number = q.company_number)
)
select section, stage, companies,
       denominator as denominator_companies,
       round(100.0 * companies / nullif(denominator, 0), 1) as percent_of_denominator,
       notes
from stages
order by sort_order;
