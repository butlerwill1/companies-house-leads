-- One row per crawled website: which companies use it, whether the crawl worked,
-- and the marketing tools and conversion paths found on it (web stage W2:
-- scripts/website_analysis/web_detect.py writes web_sites, from the pages the crawl cached).
--
-- Read the flags like this:
--   has_google_ads_tag          a Google Ads tag is on the site or in its Tag Manager container
--   has_ads_conversion_event    ... and it records conversions (what an ad produced)
--   has_ads_remarketing         ... and it follows visitors who leave
--   crm_vendors                 HubSpot, Salesforce/Pardot, Zoho and similar: leads tracked after the click
--   call_tracking_vendor        phone leads are measured (CallRail, Mediahawk, Ruler...)
--   crawl_status                ok / thin (JavaScript-built) / blocked (site refused us) / unreachable / parked
--
-- Uses the newest crawl and rule version. A site shared by two companies appears
-- once, with both names in `companies`.

select
    s.domain,
    group_concat(distinct co.company_name) as companies,
    s.crawl_status,
    s.pages_fetched,
    s.platform,
    s.has_google_ads_tag,
    s.has_ads_conversion_event,
    s.has_ads_remarketing,
    s.has_consent_mode,
    s.crm_vendors,
    s.call_tracking_vendor,
    s.social_pixels,
    s.email_vendors,
    s.optimisation_vendors,
    s.has_contact_form,
    s.has_click_to_call,
    s.has_booking,
    s.has_checkout,
    s.has_live_chat,
    s.review_widget,
    s.landing_page_count,
    s.agency_credit,
    s.copyright_year
from web_sites s
left join company_web_identity_current i on i.domain = s.domain and i.role = 'main'
left join companies co on co.company_number = i.company_number
where s.crawl_version = (select max(crawl_version) from web_sites)
  and s.rule_version = (select max(rule_version) from web_sites)
group by s.domain
order by s.crawl_status, s.has_google_ads_tag desc, s.domain;
