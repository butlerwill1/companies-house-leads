/**
 * Browser evidence collector for PPC lead qualification.
 *
 * Input is a CSV of shortlisted companies. For each company, this script
 * searches the web, filters out Companies House/profile sites, chooses the
 * best likely trading website, visits it, and records page-level signals used
 * later by scripts/analysis/ppc_pilot_report.py and
 * scripts/analysis/ch_website_investigations.py.
 *
 * Run from the repository root so relative input/output paths resolve against
 * the project workspace.
 */

import fs from "node:fs/promises";

const EXCLUDED_DOMAINS = new Set([
  "bing.com",
  "company-information.service.gov.uk",
  "find-and-update.company-information.service.gov.uk",
  "companycheck.co.uk",
  "companieshouse.gov.uk",
  "companieslist.co.uk",
  "companiesintheuk.co.uk",
  "companydirectorcheck.com",
  "companysearchesmadesimple.com",
  "endole.co.uk",
  "open.endole.co.uk",
  "ico.org.uk",
  "linkedin.com",
  "uk.linkedin.com",
  "facebook.com",
  "instagram.com",
  "x.com",
  "twitter.com",
  "youtube.com",
  "bizdb.co.uk",
  "bizseek.co.uk",
  "dirtyspoon.uk",
  "checkasalary.co.uk",
]);

const BAD_TITLE_PHRASES = [
  "companies house",
  "company summary",
  "company profile",
  "free company check",
  "overview - find and update",
  "branches across the uk",
  "food hygiene ratings",
];

const SUSPICIOUS_DOMAIN_PARTS = [
  "outlet",
  "store",
  "stores",
  "sale",
];

const COMPANY_STOPWORDS = new Set([
  "a",
  "and",
  "co",
  "company",
  "group",
  "holdco",
  "holdings",
  "international",
  "limited",
  "ltd",
  "newco",
  "plc",
  "services",
  "service",
  "solutions",
  "the",
  "uk",
]);

function normalizeWhitespace(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim();
}

function tokenizeCompanyName(companyName) {
  return normalizeWhitespace(companyName)
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .split(/\s+/)
    .filter((token) => token && token.length > 1 && !COMPANY_STOPWORDS.has(token));
}

function buildSearchQueries(companyName) {
  const tokens = tokenizeCompanyName(companyName);
  const brandQuery = tokens.slice(0, 4).join(" ");
  const queries = [companyName];
  if (brandQuery && brandQuery.toLowerCase() !== companyName.toLowerCase()) {
    queries.push(brandQuery);
  }
  return [...new Set(queries)];
}

function normalizeDomainText(value) {
  return normalizeWhitespace(value)
    .toLowerCase()
    .replace(/\s*[›>].*$/u, "")
    .replace(/^https?:\/\//, "")
    .replace(/^www\./, "")
    .replace(/\/.*$/, "");
}

function hostnameFromUrl(value) {
  try {
    const url = new URL(value);
    return url.hostname.replace(/^www\./, "").toLowerCase();
  } catch {
    return "";
  }
}

function decodeBingTarget(value) {
  try {
    const url = new URL(value);
    if (!/bing\.com$/i.test(url.hostname)) {
      return value;
    }
    const encoded = url.searchParams.get("u");
    if (!encoded || !encoded.startsWith("a1")) {
      return value;
    }
    const base64 = encoded
      .slice(2)
      .replace(/-/g, "+")
      .replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    const decoded = atob(padded);
    if (decoded.startsWith("http://") || decoded.startsWith("https://")) {
      return decoded;
    }
    return value;
  } catch {
    return value;
  }
}

function isExcludedDomain(domain) {
  if (!domain) {
    return false;
  }
  if (EXCLUDED_DOMAINS.has(domain)) {
    return true;
  }
  for (const blocked of EXCLUDED_DOMAINS) {
    if (domain === blocked || domain.endsWith(`.${blocked}`)) {
      return true;
    }
  }
  return false;
}

function resultText(result) {
  return [result.title, result.snippet, result.domain, result.hostname].join(" ").toLowerCase();
}

function scoreResult(result, companyName) {
  const text = resultText(result);
  const tokens = tokenizeCompanyName(companyName);
  const domain = result.hostname || normalizeDomainText(result.domain);

  if (isExcludedDomain(domain)) {
    return -1000;
  }
  for (const phrase of BAD_TITLE_PHRASES) {
    if (text.includes(phrase)) {
      return -800;
    }
  }

  let score = 0;
  for (const token of tokens) {
    if (text.includes(token)) {
      score += 25;
    }
    if (domain.includes(token)) {
      score += 20;
    }
  }

  if (/shop|buy|product|services|contact|about|official|home/.test(text)) {
    score += 15;
  }
  if (/company|overview|profile|directory|rating|branch|salary/.test(text)) {
    score -= 50;
  }
  if (SUSPICIOUS_DOMAIN_PARTS.some((part) => domain.includes(part))) {
    score -= 35;
  }
  if (tokens.length > 0) {
    const joined = tokens.join("");
    if (joined.length >= 6 && domain.replace(/[^a-z0-9]/g, "").includes(joined.slice(0, 12))) {
      score += 40;
    }
  }

  return score;
}

function pickBestResult(results, companyName) {
  const scored = results
    .map((result) => ({ ...result, score: scoreResult(result, companyName) }))
    .sort((a, b) => b.score - a.score);
  return scored.filter((result) => result.score >= 20);
}

async function extractSearchResults(tab) {
  return await tab.playwright.evaluate(() => {
    const rows = Array.from(document.querySelectorAll("li.b_algo")).slice(0, 8);
    return rows.map((row) => {
      const link = row.querySelector("h2 a");
      const snippetNode = row.querySelector("p");
      const domainNode = row.querySelector("cite, .b_attribution");
      return {
        title: (link?.textContent || "").trim(),
        href: link?.href || "",
        snippet: (snippetNode?.textContent || "").trim(),
        domain: (domainNode?.textContent || "").trim(),
      };
    });
  });
}

async function searchResultsForCompany(tab, companyName) {
  const combined = [];
  const seen = new Set();

  for (const query of buildSearchQueries(companyName)) {
    const searchUrl = `https://www.bing.com/search?q=${encodeURIComponent(query)}`;
    await tab.goto(searchUrl);
    await tab.playwright.waitForLoadState({ state: "domcontentloaded", timeoutMs: 15000 });
    await tab.playwright.waitForTimeout(2000);
    const rawResults = await extractSearchResults(tab);
    const results = rawResults.map((result) => ({
      ...result,
      target_url: decodeBingTarget(result.href),
      hostname: normalizeDomainText(result.domain) || hostnameFromUrl(decodeBingTarget(result.href)),
    }));
    for (const result of results) {
      const key = `${result.target_url}|${result.title}`;
      if (!seen.has(key)) {
        seen.add(key);
        combined.push(result);
      }
    }
  }

  const candidates = pickBestResult(combined, companyName);
  return {
    queries: buildSearchQueries(companyName),
    results: combined,
    candidates,
  };
}

async function extractWebsite(tab) {
  return await tab.playwright.evaluate(() => {
    const metaDescription =
      document.querySelector('meta[name="description"]')?.getAttribute("content") || "";
    const ogDescription =
      document.querySelector('meta[property="og:description"]')?.getAttribute("content") || "";

    const navLinks = Array.from(document.querySelectorAll("nav a, header a"))
      .map((node) => (node.textContent || "").trim())
      .filter(Boolean)
      .slice(0, 30);

    const ctas = Array.from(document.querySelectorAll("a, button, input[type='submit']"))
      .map((node) => {
        if (node.tagName === "INPUT") {
          return node.value || "";
        }
        return (node.textContent || "").trim();
      })
      .filter(Boolean)
      .filter((text) => /shop|book|quote|contact|buy|order|demo|trial|find out more|apply|sign in/i.test(text))
      .slice(0, 20);

    const bodySample = (document.body?.innerText || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 3500);

    const signalText = [metaDescription, ogDescription, bodySample, ...navLinks, ...ctas].join(" ").toLowerCase();

    return {
      final_url: window.location.href,
      title: document.title || "",
      meta_description: metaDescription,
      og_description: ogDescription,
      h1s: Array.from(document.querySelectorAll("h1"))
        .map((node) => (node.textContent || "").trim())
        .filter(Boolean)
        .slice(0, 5),
      nav_links: navLinks,
      ctas,
      body_sample: bodySample,
      has_checkout: /\bcheckout\b|\bbasket\b|\bcart\b|\badd to cart\b|\bshop all\b/.test(signalText),
      has_store_locator: /\bstore locator\b|\bfind a store\b|\bbranch locator\b/.test(signalText),
      has_quote_form: /\bget a quote\b|\brequest a quote\b|\bquote\b/.test(signalText),
      has_booking: /\bbook now\b|\bbook online\b|\bmake a booking\b/.test(signalText),
      has_demo: /\bbook a demo\b|\brequest demo\b|\bdemo\b|\bfree trial\b/.test(signalText),
      has_finance: /\bfinance\b|\bloan\b|\bmortgage\b|\bcredit\b|\basset finance\b/.test(signalText),
    };
  });
}

async function visitWebsite(siteTab, result) {
  if (!result?.target_url && !result?.href) {
    return null;
  }
  await siteTab.goto(result.target_url || result.href);
  try {
    await siteTab.playwright.waitForLoadState({ state: "domcontentloaded", timeoutMs: 15000 });
  } catch {
    // Continue even if the initial page is slow; the DOM may still be usable.
  }
  await siteTab.playwright.waitForTimeout(2500);
  return await extractWebsite(siteTab);
}

function looksSuspicious(result) {
  const domain = result.hostname || "";
  return SUSPICIOUS_DOMAIN_PARTS.some((part) => domain.includes(part));
}

function isUsableWebsite(website) {
  if (!website?.final_url) {
    return false;
  }
  if (website.final_url === "about:blank") {
    return false;
  }
  if (website.final_url.startsWith("data:text/html")) {
    return false;
  }
  if (!website.title && !website.meta_description && !website.body_sample) {
    return false;
  }
  return true;
}

export async function runPilotBatch({
  browser,
  inputPath,
  outputPath,
  limit = 50,
  startAt = 0,
}) {
  const raw = await fs.readFile(inputPath, "utf8");
  const companies = JSON.parse(raw).slice(startAt, startAt + limit);
  const outputs = [];

  for (const company of companies) {
    const searchTab = await browser.tabs.new();
    const row = {
      company_number: company.company_number,
      company_name: company.company_name,
      sic_1: company.sic_1,
      sic_label: company.sic_label,
      account_category: company.account_category,
      turnover: company.turnover,
      estimated_monthly_ppc_spend: company.estimated_monthly_ppc_spend,
    };

    try {
      const search = await searchResultsForCompany(searchTab, company.company_name);
      row.search_queries = search.queries;
      row.search_results = search.results;
      row.candidates = search.candidates;

      if (!search.candidates.length) {
        row.status = "no_result";
      } else {
        let lastError = "";
        for (const candidate of search.candidates.slice(0, 6)) {
          const siteTab = await browser.tabs.new();
          try {
            const website = await visitWebsite(siteTab, candidate);
            if (isUsableWebsite(website)) {
              row.chosen_result = candidate;
              row.website = website;
              row.status = looksSuspicious(candidate) ? "ok_suspicious_domain" : "ok";
              await siteTab.close();
              break;
            }
            lastError = "empty_or_error_page";
          } catch (error) {
            lastError = error instanceof Error ? error.message : String(error);
          } finally {
            try {
              await siteTab.close();
            } catch {
              // Ignore close failures.
            }
          }
        }

        if (!row.status) {
          row.status = "site_error";
          row.error = lastError;
        }
      }
    } catch (error) {
      row.status = "error";
      row.error = error instanceof Error ? error.message : String(error);
    } finally {
      try {
        await searchTab.close();
      } catch {
        // Ignore close failures.
      }
    }

    outputs.push(row);
    await fs.writeFile(outputPath, JSON.stringify(outputs, null, 2), "utf8");
  }

  return outputs;
}
