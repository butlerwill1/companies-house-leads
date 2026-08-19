-- The whole filed narrative for a company on one row: every section_key
-- pivoted into its own column, so you can read what a company says about
-- itself without scrolling through nine separate rows.
--
-- Source is narrative_sections (the text) joined through narrative_runs
-- (one row per parsed document) to the company. 2,960 of 8,169 companies
-- have narrative, because the extraction backfill was only run over
-- companies that had turnover.
--
-- In DB Browser, click a cell to read the full text in the side panel --
-- going_concern in particular runs to a couple of thousand words.
--
-- `junk_sections` counts sections where iXBRL tag soup leaked in instead of
-- prose (director tags, contextRef attributes) rather than readable text.
-- It affects roughly 17% of principal_activity rows and is a known bug in
-- the section-boundary detection, not a property of the filing.
--
-- Only the most recent parsed document per company is used. Since the
-- history backfill, a company can have several narrative_runs (one per
-- filing), and pivoting across all of them would silently mix text from
-- different years into one row.
--
-- Set the company number below, or comment out the where clause to sweep
-- everything (add a LIMIT if you do).

with latest_run as (
    select company_number, max(id) as narrative_run_id
    from narrative_runs
    group by company_number
),
latest_financials as (
    -- One row per company. Without this the multi-year history fans the
    -- join out, inflating counts and picking an arbitrary year's turnover.
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
    f.turnover,
    f.employees,

    -- how much usable text exists, at a glance
    count(ns.id) as sections,
    sum(case when ns.section_text like '%bus:Director%'
              or ns.section_text like '%contextRef%'
              or ns.section_text like '%xbrli%' then 1 else 0 end) as junk_sections,

    max(case when ns.section_key = 'principal_activity'    then ns.section_text end) as principal_activity,
    max(case when ns.section_key = 'business_review'       then ns.section_text end) as business_review,
    max(case when ns.section_key = 'strategic_report'      then ns.section_text end) as strategic_report,
    max(case when ns.section_key = 'directors_report'      then ns.section_text end) as directors_report,
    max(case when ns.section_key = 'principal_risks'       then ns.section_text end) as principal_risks,
    max(case when ns.section_key = 'future_developments'   then ns.section_text end) as future_developments,
    max(case when ns.section_key = 'results_and_dividends' then ns.section_text end) as results_and_dividends,
    max(case when ns.section_key = 'post_balance_sheet'    then ns.section_text end) as post_balance_sheet,
    max(case when ns.section_key = 'going_concern'         then ns.section_text end) as going_concern

from latest_run lr
join narrative_sections ns on ns.narrative_run_id = lr.narrative_run_id
join companies c on c.company_number = lr.company_number
left join sic_groups g on g.sic_code = c.sic_code_primary
left join latest_financials f on f.company_number = c.company_number
where c.company_number = '00482197'
group by c.company_number
order by f.turnover desc;
