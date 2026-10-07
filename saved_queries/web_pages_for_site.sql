-- The pages the crawl fetched from one website (up to 25), and what was
-- extracted from each: how it was fetched, whether it worked, its size, forms,
-- phone numbers and calls to action. Use it to see what the crawl actually read
-- before trusting the site's summary (web_sites_overview.sql).
--
--   page_kind        home / about / service / location / landing / contact / privacy ...
--   fetched_with     http (a plain download) or browser (the Playwright fallback)
--   fetch_error      blocked (403), challenge page, parked, not html, ... (null when fine)
--   in_navigation    1 = linked from the menu; a page that is NOT (and is noindex) is
--                    usually a landing page built for ads
--
-- Edit the domain below before running.

select
    page_kind,
    url,
    fetched_with,
    status_code,
    fetch_error,
    word_count,
    form_count,
    form_providers,
    tel_link_count,
    phone_numbers,
    cta_phrases,
    price_mentions,
    in_navigation,
    noindex,
    schema_types,
    company_number_found,
    title
from web_pages
where domain = 'improveasy.com'
  and crawl_version = (select max(crawl_version) from web_pages where domain = 'improveasy.com')
order by case page_kind when 'home' then 0 when 'sitemap' then 98 when 'gtm_container' then 99 else 1 end,
         page_kind, url;
