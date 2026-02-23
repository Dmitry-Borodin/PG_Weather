/**
 * PG Weather Triage — Headless scraper for soaring-specific sources.
 * Uses Playwright (Chromium) to intercept XHR/fetch on JS-heavy sites.
 *
 * Usage:
 *   deno run -A scripts/scraper.ts \
 *     --date 2026-02-24 \
 *     --locations '[{"key":"lenggries","lat":47.68,"lon":11.57},...]' \
 *     --sources meteo_parapente,xccontest
 *
 * Outputs JSON to stdout (one object per line = JSONL).
 * Errors go to stderr.
 */

import { chromium, type Page, type BrowserContext } from "npm:playwright@1.52.0";
import { parseArgs } from "jsr:@std/cli@1/parse-args";

// ──────────────────────────────────────────────
// CLI
// ──────────────────────────────────────────────

const args = parseArgs(Deno.args, {
  string: ["date", "locations", "sources"],
  default: { sources: "meteo_parapente" },
});

if (!args.date || !args.locations) {
  console.error("Usage: scraper.ts --date YYYY-MM-DD --locations JSON --sources src1,src2");
  Deno.exit(1);
}

interface Location {
  key: string;
  lat: number;
  lon: number;
}

const date: string = args.date;
const locations: Location[] = JSON.parse(args.locations);
const sources: string[] = args.sources.split(",").map((s: string) => s.trim());

// ──────────────────────────────────────────────
// Meteo-Parapente scraper
// ──────────────────────────────────────────────
// The site loads a forecast when you click on the map.
// We intercept the XHR to their API to grab the JSON payload.
// API pattern: POST or GET to api.meteo-parapente.com with sounding data.

async function scrapeMeteoParapente(
  context: BrowserContext,
  loc: Location,
  date: string,
): Promise<Record<string, unknown> | null> {
  const page = await context.newPage();
  const captured: Record<string, unknown>[] = [];
  const allRequests: string[] = [];

  // Intercept ALL responses to discover API patterns
  page.on("response", async (response) => {
    const url = response.url();
    allRequests.push(`${response.status()} ${url.substring(0, 120)}`);

    // Capture any JSON response from the site's domain or known API patterns
    if (response.status() === 200) {
      try {
        const ct = response.headers()["content-type"] || "";
        if (
          (url.includes("meteo-parapente") || url.includes("meteoparapente")) &&
          !url.endsWith(".js") && !url.endsWith(".css") &&
          !url.endsWith(".png") && !url.endsWith(".svg") &&
          !url.endsWith(".woff") && !url.endsWith(".woff2") &&
          (ct.includes("json") || ct.includes("octet") || ct.includes("protobuf") ||
           ct.includes("msgpack") || ct.includes("application/"))
        ) {
          const body = await response.body().catch(() => null);
          if (body && body.length > 50) {
            // Try JSON first
            const text = new TextDecoder().decode(body);
            try {
              const json = JSON.parse(text);
              captured.push({ url, type: "json", data: json });
            } catch {
              // Binary data — store base64 snippet
              captured.push({
                url,
                type: ct,
                size: body.length,
                preview: text.substring(0, 200),
              });
            }
          }
        }
      } catch {
        // ignore
      }
    }
  });

  try {
    console.error(`  [meteo-parapente] Loading for ${loc.key} (${loc.lat},${loc.lon})...`);

    // Navigate to the location
    const mapUrl = `https://meteo-parapente.com/#/${loc.lat.toFixed(4)},${loc.lon.toFixed(4)},11`;
    await page.goto(mapUrl, { waitUntil: "networkidle", timeout: 45_000 });
    await page.waitForTimeout(3_000);

    // Click on the map center to trigger forecast
    const viewport = page.viewportSize() || { width: 1280, height: 720 };
    const cx = viewport.width / 2;
    const cy = viewport.height / 2;

    // Try clicking the map area (not on UI elements)
    await page.mouse.click(cx, cy);
    await page.waitForTimeout(5_000);

    // If no data captured, try a second click slightly offset
    if (captured.length === 0) {
      console.error(`  [meteo-parapente] Retrying click...`);
      await page.mouse.click(cx + 10, cy + 10);
      await page.waitForTimeout(5_000);
    }

    // Try to extract visible forecast data from the DOM
    const domData = await page.evaluate(() => {
      const result: Record<string, unknown> = {};

      // Windgram table data
      const windgram = document.querySelector("[class*='windgram'], [class*='Windgram']");
      if (windgram) {
        result.windgram_html = windgram.innerHTML.substring(0, 2000);
        result.windgram_text = windgram.textContent?.trim().substring(0, 500);
      }

      // Sounding data
      const sounding = document.querySelector("[class*='sounding'], [class*='Sounding']");
      if (sounding) {
        result.sounding_text = sounding.textContent?.trim().substring(0, 500);
      }

      // Any visible forecast panel
      const panels = document.querySelectorAll("[class*='forecast'], [class*='panel'], [class*='detail']");
      const panelTexts: string[] = [];
      panels.forEach((p) => {
        const t = p.textContent?.trim();
        if (t && t.length > 20 && t.length < 2000) panelTexts.push(t);
      });
      if (panelTexts.length) result.panels = panelTexts;

      // Canvas elements (thermal/wind maps are often canvas)
      const canvases = document.querySelectorAll("canvas");
      result.canvas_count = canvases.length;

      return result;
    }).catch(() => ({}));

    // Log what we found for debugging
    console.error(`  [meteo-parapente] Captured: ${captured.length} API responses, DOM keys: ${Object.keys(domData).join(",")}`);
    if (captured.length === 0) {
      console.error(`  [meteo-parapente] API requests seen (last 10):`);
      allRequests.slice(-10).forEach((r) => console.error(`    ${r}`));
    }

    if (captured.length === 0 && Object.keys(domData).length === 0) {
      return null;
    }

    return {
      source: "meteo_parapente",
      location: loc.key,
      date,
      captured_api: captured,
      dom_data: domData,
    };
  } catch (err) {
    console.error(`  [meteo-parapente] Error for ${loc.key}: ${err}`);
    return null;
  } finally {
    await page.close();
  }
}

// ──────────────────────────────────────────────
// XContest recent flights scraper
// ──────────────────────────────────────────────
// Fetches recent flights near each location as a "sanity check" —
// were people actually flying?

async function scrapeXContest(
  context: BrowserContext,
  loc: Location,
  date: string,
): Promise<Record<string, unknown> | null> {
  const page = await context.newPage();

  try {
    console.error(`  [xccontest] Loading flights near ${loc.key}...`);

    // XContest flight search URL near a point
    const url =
      `https://www.xcontest.org/world/en/flights-search/?` +
      `filter[point]=${loc.lat}+${loc.lon}&` +
      `filter[radius]=50000&` +
      `filter[date_mode]=dmy&` +
      `filter[date]=${date}&` +
      `filter[mode]=START`;

    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30_000 });
    await page.waitForTimeout(3_000);

    // Extract flights from the results table
    const flights = await page.evaluate(() => {
      const rows = document.querySelectorAll("table.XClist tbody tr, .flight-row, [class*='flight']");
      const result: Record<string, string>[] = [];
      rows.forEach((row) => {
        const cells = row.querySelectorAll("td");
        if (cells.length >= 4) {
          result.push({
            pilot: cells[1]?.textContent?.trim() || "",
            launch: cells[2]?.textContent?.trim() || "",
            distance: cells[3]?.textContent?.trim() || "",
            type: cells[4]?.textContent?.trim() || "",
            raw: row.textContent?.trim().substring(0, 200) || "",
          });
        }
      });
      return result;
    });

    // Also get flight count
    const summary = await page.evaluate(() => {
      const el = document.querySelector(".XCinfo, .results-summary, [class*='result']");
      return el?.textContent?.trim() || null;
    });

    return {
      source: "xccontest",
      location: loc.key,
      date,
      flights_near: flights.slice(0, 20),
      summary,
      flight_count: flights.length,
    };
  } catch (err) {
    console.error(`  [xccontest] Error for ${loc.key}: ${err}`);
    return null;
  } finally {
    await page.close();
  }
}

// ──────────────────────────────────────────────
// ALPTHERM scraper (Austro Control — public part)
// ──────────────────────────────────────────────
// ALPTHERM thermal quality data is publicly visible on the overview page
// at https://flugwetter.austrocontrol.at/ (the detail needs login,
// but the overview map/table is accessible).

async function scrapeAlptherm(
  context: BrowserContext,
  _loc: Location,
  _date: string,
): Promise<Record<string, unknown> | null> {
  const page = await context.newPage();

  try {
    console.error("  [alptherm] Loading Austro Control flugwetter...");

    // Try the public overview page
    await page.goto("https://flugwetter.austrocontrol.at/", {
      waitUntil: "domcontentloaded",
      timeout: 30_000,
    });
    await page.waitForTimeout(3_000);

    // Check if there's a login wall
    const isLoginPage = await page.evaluate(() => {
      const body = document.body.textContent || "";
      return body.includes("Login") && body.includes("Passwort") && body.length < 5000;
    });

    if (isLoginPage) {
      console.error("  [alptherm] Login required — skipping");
      return { source: "alptherm", error: "login_required" };
    }

    // Try to extract any thermal quality data
    const data = await page.evaluate(() => {
      const tables = document.querySelectorAll("table");
      const result: Record<string, string>[] = [];
      tables.forEach((t) => {
        const rows = t.querySelectorAll("tr");
        rows.forEach((r) => {
          const cells = r.querySelectorAll("td, th");
          if (cells.length >= 2) {
            result.push(
              Object.fromEntries(
                Array.from(cells).map((c, i) => [`col${i}`, c.textContent?.trim() || ""]),
              ),
            );
          }
        });
      });

      // Also grab any images (ALPTHERM often shows thermal quality as a map image)
      const images = Array.from(document.querySelectorAll("img")).filter((img) => {
        const src = img.src || "";
        return (
          src.includes("therm") || src.includes("alptherm") || src.includes("soaring")
        );
      }).map((img) => img.src);

      return { tables: result, thermal_images: images, body_length: document.body.textContent?.length };
    });

    return {
      source: "alptherm",
      ...data,
    };
  } catch (err) {
    console.error(`  [alptherm] Error: ${err}`);
    return null;
  } finally {
    await page.close();
  }
}

// ──────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────

const SCRAPERS: Record<
  string,
  (ctx: BrowserContext, loc: Location, date: string) => Promise<Record<string, unknown> | null>
> = {
  meteo_parapente: scrapeMeteoParapente,
  xccontest: scrapeXContest,
  alptherm: scrapeAlptherm,
};

async function main() {
  console.error(`Scraper starting: date=${date}, ${locations.length} locations, sources=${sources.join(",")}`);

  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
  });

  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    locale: "en-US",
    userAgent:
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
  });

  const results: Record<string, unknown>[] = [];

  for (const source of sources) {
    const scraper = SCRAPERS[source];
    if (!scraper) {
      console.error(`  Unknown source: ${source}`);
      continue;
    }

    // ALPTHERM is location-independent (Austria-wide overview)
    if (source === "alptherm") {
      const result = await scraper(context, locations[0], date);
      if (result) results.push(result);
      continue;
    }

    // Per-location scrapers
    for (const loc of locations) {
      const result = await scraper(context, loc, date);
      if (result) results.push(result);
    }
  }

  await context.close();
  await browser.close();

  // Output all results as a single JSON array to stdout
  console.log(JSON.stringify(results));
  console.error(`Scraper finished: ${results.length} results collected`);
}

await main();
