-- What the site-profile model (web stage W3) said about each company, beside
-- the evidence checks, to review its answers. One row per company.
--
-- Each quoted label (customer type, how customers convert, area served) must be
-- backed by a verbatim quote from the site text the model was shown:
--   *_quote_valid = 1  the quote was found in what the model was shown (site text,
--                      principal activity or Maps category)
--   *_quote_valid = 0  it was not, even after one retry: treat the value with care
--   null               the model said "unclear", so there is no quote to check
--   *_quote_match      how it was found: exact / table_row / fuzzy (one ordinary word
--                      in eight may differ) / joined (passages joined with "...")
-- `attempts` = 2 when a quote failed and the model was asked once more; the first
-- answer is kept in `first_attempt`. `problem` lists anything that made the profile incomplete.
--
-- category_source: google_listing = Google's own category from the company's Maps
-- listing; model_assigned = chosen by the model from a shortlist of Google's categories.
-- Newest profile per company only.

select
    co.company_name,
    p.domain,
    p.summary,
    p.google_category,
    p.category_source,
    p.customer_type,
    p.customer_type_quote_valid,
    p.conversion_action,
    p.conversion_action_quote_valid,
    p.geography,
    p.main_town,
    p.geography_quote_valid,
    p.urgency,
    p.ticket_band,
    p.channel_fit,
    p.seed_keywords,
    p.problem,
    p.attempts,
    p.customer_type_quote_match,
    p.conversion_action_quote_match,
    p.geography_quote_match,
    p.customer_type_quote,
    p.conversion_action_quote,
    p.geography_quote,
    p.model,
    p.profile_version
from company_web_profile p
join companies co on co.company_number = p.company_number
where p.profiled_at = (
    select max(p2.profiled_at) from company_web_profile p2 where p2.company_number = p.company_number)
order by (p.problem is not null) desc, co.company_name;
