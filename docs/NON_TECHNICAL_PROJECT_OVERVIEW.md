# Companies House Leads: a non-technical overview

## What this project is

Companies House Leads is a research tool for finding UK businesses that may
benefit from help with online advertising, particularly pay-per-click (PPC)
campaigns. It turns public Companies House records into a more useful picture
of each business: what it does, whether it appears to be genuinely trading,
how large it is, how its finances are changing, and how it seems to win and
serve customers.

Companies House contains a huge amount of useful information, but it is spread
across bulk data files, company profiles, filing histories and annual accounts.
Some accounts are structured web pages; others are PDFs that are awkward to
search or compare. This project brings those sources together in one local
database and standardises the most useful facts.

The aim is not simply to produce a long list of companies. It is to help answer
more practical questions:

- Is this an active trading business or just a holding company or dormant
  entity?
- Is it large enough to be a realistic prospect?
- Are sales, profits, assets and employee numbers growing or shrinking?
- Does the business sell to consumers, other businesses or the public sector?
- Do customers find it through search, local discovery, contracts, repeat
  relationships or an online platform?
- Is PPC likely to fit the way the company actually attracts customers?

This should make prospecting more focused. Instead of treating every company
with the right industry code as equally promising, the user can concentrate on
businesses whose recent performance and commercial model suggest a genuine
opportunity.

## What it can do

The project can filter the national Companies House dataset into a more
relevant pool of leads, then enrich each company using official Companies
House information. It records basics such as company name, number, status,
location, incorporation date and industry codes, together with accounts and
filing history.

From annual accounts it can extract and compare figures including:

- turnover;
- gross profit and gross margin;
- operating profit or loss and operating margin;
- profit after tax;
- cash;
- net assets; and
- average employee numbers.

It keeps the currency reported in the accounts and can separately convert
foreign-currency figures into pounds for comparison. It also stores where the
information came from, rather than presenting an unexplained score.

Historical filings are especially valuable. A single set of accounts can give
a misleading impression if a business had an unusually good or bad year.
Several years reveal the direction of travel: recovery after a downturn,
steady growth, declining margins, rising staff numbers, or a weakening balance
sheet. The project includes queries and a read-only assistant interface for
finding companies and retrieving these histories without manually opening
every filing.

## A real example lead: Café Andaluz Restaurants Limited

The following figures were extracted from public filed accounts held in the
local project database. They are rounded for readability. This is a useful
example because Café Andaluz is a consumer-facing, multi-location restaurant
business with four consecutive financial years in the database. It is also the
kind of company for which local search, online reservations and seasonal
campaigns can produce measurable commercial results.

| Financial year | Turnover | Change | Gross margin | Profit after tax | Cash | Net assets | Employees |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2022 | £16.42m | — | 75.7% | £4.08m | £0.72m | £0.10m | 247 |
| 2023 | £15.65m | -4.7% | 74.4% | £2.80m | £0.76m | £0.11m | 298 |
| 2024 | £16.72m | +6.8% | 75.4% | £2.39m | £0.39m | £0.09m | 290 |
| 2025 | £17.16m | +2.6% | 76.3% | £2.18m | £0.68m | £0.11m | 298 |

The company remained profitable throughout the four years. Turnover dipped in
2023, recovered in 2024 and reached £17.16 million in 2025, around 4.5% above
2022. It employed an average of 298 people in 2025, compared with 247 in 2022.
These figures describe an established business with the scale to invest in
customer acquisition rather than a very small or dormant prospect.

The history also gives a credible reason for a commercial conversation. Profit
after tax reduced from £4.08 million in 2022 to £2.18 million in 2025 even as
turnover recovered. The latest filed narrative identifies higher food,
beverage and energy costs, together with competition from other restaurants,
as the main risks. Digital advertising cannot solve input-cost pressure, but
well-measured campaigns could help protect demand, fill quieter periods and
promote the most valuable bookings without relying on vague promises about
“more awareness”.

The business profile extractor adds the missing commercial context. For Café
Andaluz it identifies an active trading business, a business-to-consumer
customer base and a **local-service demand model**; it also confirms that the
filed description agrees with the restaurant industry code. This makes it much
more plausible as a digital advertising lead than the construction contractor
used in the previous version of this document.

The [Café Andaluz website](https://www.cafeandaluz.com/) currently presents
seven restaurants across Edinburgh, Glasgow, Aberdeen and Newcastle. Visitors
can reserve a table by location, make a group or private-dining enquiry, buy a
gift voucher and join a loyalty scheme. Those are clear conversion points that
could support location-specific search campaigns, event and seasonal
promotions, remarketing, and reliable tracking from advertising click to
booking or enquiry. This does not prove that the company is unhappy with its
current marketing; it shows why it is a sensible prospect for a relevant,
evidence-led approach.

## The VLM financial extractor

Many Companies House accounts can be read directly from structured tags, which
is the preferred route. However, a significant number are available only as
PDFs or scanned-looking documents. Ordinary database tools cannot reliably
understand the tables and page layouts in those files.

The project's vision-language-model (VLM) extractor fills that gap. It first
locates the pages containing the income statement, balance sheet and other
financial statements. It then reads the selected pages at higher quality,
captures the figures with their labels, currency, scale and supporting
evidence, and converts them into the same standard metrics used for structured
accounts.

This matters because PDF-only companies no longer have to be excluded or
entered by hand. The extractor can broaden coverage while retaining evidence
that can be checked. Its results are tested against manually reviewed example
accounts, and the evaluation process records results company by company rather
than relying only on one headline accuracy number.

## The business profile extractor

Financials show the size and direction of a company, but they do not explain
how the business works. The business profile extractor reads the narrative
sections of filed accounts and turns them into a short commercial profile.

It identifies what the company does, whether it appears to be trading, who its
customers are, what it delivers, the geographic market it serves, and how
demand reaches it. It also checks whether the company's official industry code
agrees with the more detailed description in its accounts.

Each conclusion must be supported by text from the filing. If the evidence is
not strong enough, the extractor can say “unclear” instead of inventing an
answer. It has been built and is being assessed against a 57-company,
human-reviewed test set before broad use.

This profile is valuable for both selection and personalisation. It can help
remove dormant companies, investment vehicles and unsuitable business models;
separate consumer-search opportunities from relationship-led B2B firms; and
give a salesperson a more relevant reason for contacting a prospect. Combined
with multi-year accounts, it moves the project towards a practical view of
both **whether a company is worth approaching** and **what kind of approach is
likely to make sense**.

## Where it is heading

The working foundation is already in place: lead filtering, Companies House
enrichment, structured and PDF financial extraction, multi-year histories,
commercial profiling, quality evaluations and a query interface. The next
value comes from expanding carefully—processing more suitable companies,
continuing to measure extraction quality, and turning the strongest financial
and business-profile signals into a clear shortlist for outreach.

The figures in this document come from public filings and the local database
snapshot checked on 21 August 2026. The current booking and location details
were checked against the company's public website on the same date. They are
useful for research and lead qualification, but important commercial decisions
should still be checked against the original filed accounts and a current
review of the prospect's marketing.
