-- How common is each technology across the crawled websites? One row per
-- technology, most-used first, with the sites that use it. Useful for judging
-- which signals separate companies (a tool every site has says nothing; one only
-- a few have, such as call tracking or a CRM, is a real marker of sophistication).
--
-- Uses the newest rule version. `sites_total` is the number of crawled websites
-- the share is out of (blocked and unreachable sites included: their tools could
-- not be read).

with latest as (
    select max(rule_version) as rule_version from web_technologies
),
total as (
    select count(*) as n from web_sites where rule_version = (select rule_version from latest)
)
select
    t.category,
    t.technology,
    count(distinct t.domain) as sites,
    (select n from total) as sites_total,
    round(100.0 * count(distinct t.domain) / (select n from total), 0) as pct_of_sites,
    sum(t.found_in = 'gtm') as only_in_tag_manager,
    group_concat(distinct t.domain) as domains
from web_technologies t
where t.rule_version = (select rule_version from latest)
group by t.category, t.technology
order by sites desc, t.category, t.technology;
