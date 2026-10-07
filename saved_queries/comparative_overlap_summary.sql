-- Match/mismatch counts from the comparative-overlap accuracy harness,
-- overall and by lead account category, to gauge how much of the
-- backfilled data is worth a second look. Periods with a null status
-- either haven't been cross-checked yet (no adjacent filing exists) or
-- predate the history backfill.

select
    'overall' as account_category,
    comparative_overlap_status,
    count(*) as periods
from financial_period_summaries
where comparative_overlap_status is not null
group by comparative_overlap_status

union all

select
    coalesce(l.account_category, 'unknown') as account_category,
    f.comparative_overlap_status,
    count(*) as periods
from financial_period_summaries f
left join leads l on l.company_number = f.company_number
where f.comparative_overlap_status is not null
group by account_category, f.comparative_overlap_status
order by account_category, comparative_overlap_status;
