-- Every technology detected on one company's website, with the evidence: what
-- text in the page (or in its Google Tag Manager container) made the code say
-- "this site uses X". Start here to check a detection, or to see exactly how a
-- company's setup looks.
--
--   found_in      page = in the site's own HTML; gtm = only inside its Tag Manager
--                 container; both = in both
--   account_ids   the tag/account ids found (Google Ads AW-..., GA4 G-..., Tag Manager
--                 GTM-..., a HubSpot portal number). Two companies sharing an id
--                 usually share an owner or an agency.
--   evidence      the matched text; evidence_url is the page or container it was in
--
-- Edit the company number below before running.

select distinct
    t.category,
    t.technology,
    t.found_in,
    t.account_ids,
    t.evidence,
    t.evidence_url
from company_web_identity_current i
join web_technologies t on t.domain = i.domain
where i.company_number = '08615712'
  and i.role = 'main'
  and t.rule_version = (select max(rule_version) from web_technologies)
order by t.category, t.technology;
