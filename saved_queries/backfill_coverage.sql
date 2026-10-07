-- How many distinct financial years each company has on record, so you can
-- see who still only has the single latest-filing snapshot from forward
-- enrichment versus who has been through the history backfill.

select
    years_of_history,
    count(*) as companies
from (
    select company_number, count(distinct financial_year) as years_of_history
    from financial_period_summaries
    where period_type = 'current' and financial_year is not null
    group by company_number
)
group by years_of_history
order by years_of_history;
