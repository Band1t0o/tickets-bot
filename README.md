# Vietnam Tickets Scraper

**File-first** flight price watcher for routes **to Vietnam** (e.g., `PRG → SGN`, `VIE → HAN`, etc.).

- ✅ Stores results as **CSV/JSON on disk** (no database)
- ✅ Tracks **only new** price snapshots (de-duped via content hash)
- ✅ **Providers** per website (extensible)
- ✅ **CLI** + **Docker** + **GitHub Actions** for cron-style runs
- ✅ Optional **Playwright** for JS-heavy sites

> ⚠️ Always check each site’s **Terms of Service** and **robots.txt**. Use reasonable rates & headers. This project is for personal/educational use.

---

## Quick start

```bash
# Python 3.11+
make install        # installs pip deps
cp .env.example .env.local && $EDITOR .env.local
make run            # scrape once
make watch          # loop with day/night intervals
```

### Docker
```bash
docker compose up --build
```

### What you get after a run
```
./data/
  2025-10-12/
    meta.json                    # run metadata
    PRG-SGN-2025-12-20_BAMBOO.csv
    PRG-SGN-2025-12-20_BAMBOO.jsonl
    PRG-SGN-2025-12-20_VIETNAM_AIRLINES.csv
    seen_offers.txt             # dedupe ledger
```

---

## Configure

Environment variables (use `.env.local` for local dev):

| Var | Example | Required | Notes |
| --- | --- | :---: | --- |
| `ORIGIN` | `PRG` or `PRG\|VIE\|BRQ` | ✅ | IATA code(s), pipe-separated for multiple |
| `DESTINATION` | `SGN` or `SGN\|HAN\|DAD` | ✅ | IATA code(s), pipe-separated for multiple |
| `DEPARTURE_DATE` | `2025-12-20` | ✅ | ISO date |
| `ARRIVAL_DATE` | `2025-12-30` | ✅ | ISO date |
| `ADULTS` | `1` | ✅ | Pax count |
| `REFRESH_INTERVAL_DAYTIME_MINUTES` | `30` |  | Watch loop interval |
| `REFRESH_INTERVAL_NIGHTTIME_MINUTES` | `120` |  | Night interval (22–06) |
| `USER_AGENT` | custom UA |  | Requests header |
| `HEADLESS` | `true` |  | Playwright headless mode |

Provider-specific vars are documented in each provider file (e.g., cookies, locale).

### Multiple Origins/Destinations

You can track prices across **multiple routes** by specifying pipe-separated (`|`) values:

```bash
# Track flights from Prague OR Vienna to Saigon OR Hanoi
ORIGIN=PRG|VIE
DESTINATION=SGN|HAN

# This will scrape 4 routes:
# - PRG → SGN
# - PRG → HAN
# - VIE → SGN
# - VIE → HAN
```

Each route combination will:
- Be scraped separately by each provider
- Generate separate CSV/JSONL files (e.g., `PRG-SGN-2026-08-01_PELIKAN.csv`, `VIE-HAN-2026-08-01_PELIKAN.csv`)
- Allow you to compare prices across different departure cities

---

## Providers

Each provider lives in `src/providers/` and implements `BaseProvider`.

### Available Providers:

**API-based (Recommended):**
- `skyscanner_api.py` – **Skyscanner API via RapidAPI** (requires API key)
  - ✅ Legal and ToS-compliant
  - ✅ Free tier: 100 calls/month
  - ✅ Stable, reliable data
  - Setup: Get API key from https://rapidapi.com/skyscanner/api/skyscanner-api

**Web Scraping (Personal use only):**
- `letuska.py` – Letuska.cz scraper (Playwright)
  - ⚠️ Personal, non-commercial use only
  - ⚠️ Respects robots.txt disallow rules
  - ⚠️ Built-in rate limiting
- `pelikan.py` – Pelikan.cz scraper (Playwright)
  - ⚠️ Personal, non-commercial use only
  - ⚠️ Respects robots.txt disallow rules
  - ⚠️ Built-in rate limiting
- `vietnam_airlines.py` – Vietnam Airlines (skeleton, needs implementation)
- `bamboo_airways.py` – Bamboo Airways (skeleton, needs implementation)

**Testing:**
- `demo_static.py` – Fully offline example to test the pipeline

Add your own provider by copying a template and registering it in `src/providers/__init__.py`.

---

## Setting up Skyscanner API (Recommended)

1. **Sign up for RapidAPI:**
   - Go to https://rapidapi.com/skyscanner/api/skyscanner-api
   - Create a free account

2. **Subscribe to Skyscanner API:**
   - Choose the **Basic (Free)** plan: 100 calls/month
   - Or choose a paid plan for more requests

3. **Get your API key:**
   - On the API page, go to the "Endpoints" tab
   - Your API key is shown in the code examples under "x-rapidapi-key"

4. **Add to your environment:**
   ```bash
   # In .env.local or .env
   SKYSCANNER_API_KEY=your_actual_api_key_here
   ```

5. **Run with Skyscanner API:**
   ```bash
   python -m src.cli scrape --provider SKYSCANNER_API
   ```

---

## CLI

```bash
# single run for all enabled providers
python -m src.cli scrape

# restrict to a specific provider (recommended)
python -m src.cli scrape --provider PELIKAN

# use multiple specific providers
python -m src.cli scrape --provider PELIKAN --provider LETUSKA

# continuous watch mode (with day/night intervals)
python -m src.cli watch
```

### Provider Usage Notes

**For API-based providers (SKYSCANNER_API):**
- Rate limits are enforced by the API provider
- Free tier: 100 calls/month = ~3 calls/day
- Use watch mode carefully to avoid exceeding limits
- Recommended: scrape once or twice per day

**For web scraping providers (LETUSKA):**
- ⚠️ **Personal use only** - do not use for commercial purposes
- Built-in delays prevent overwhelming servers
- May break if website structure changes
- Selectors may need updating - inspect the site and modify `src/providers/letuska.py`

---

## Discord Notifications

Get notified in Discord whenever new flight prices are found!

### Setup:

1. **Create a Discord webhook:**
   - Go to your Discord server
   - Server Settings → Integrations → Webhooks
   - Click "New Webhook"
   - Give it a name (e.g., "Flight Tracker") and select a channel
   - Copy the Webhook URL

2. **Add webhook to your environment:**
   ```bash
   # In .env.local
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN
   ```

3. **For GitHub Actions**, add as a secret:
   - Go to repo Settings → Secrets and variables → Actions
   - Add secret: `DISCORD_WEBHOOK_URL` with your webhook URL

### What you'll get:

When new prices are found, you'll receive a Discord message with:
- **Summary** of all new offers by route
- **Best deals** - Top 3 cheapest flights
- **Direct links** to each offer

Notifications are sent **only when new offers are found** to avoid spam.

---

## GitHub Actions (optional)

A cron workflow runs the scraper and **commits data files** back to the repo (no DB). You must set a repo `GITHUB_TOKEN` with `contents: write` (the default token works) and any site cookies/API keys as GitHub **Secrets**.

---

## Project structure

```
src/
  cli.py               # CLI entrypoint
  config.py            # env/envfile loader
  models.py            # dataclasses for offers
  storage.py           # file I/O, CSV/JSONL, dedupe ledger
  scheduler.py         # day/night refresh logic
  providers/
    base.py            # BaseProvider ABC
    __init__.py        # registry
    demo_static.py     # offline example
    bamboo_airways.py  # requests+bs4 example (skeleton)
    vietnam_airlines.py# Playwright example (skeleton)
.github/workflows/scrape.yml  # cron + commit
Dockerfile
docker-compose.yml
Makefile
.env.example
requirements.txt
```

---

## Legal & Ethics

- Respect ToS and robots.txt.
- Keep request rates low.
- Prefer public/official APIs where possible.

---

## Credits & inspiration

- Inspired by **web-scraper-nabidek-pronajmu** by @janchaloupka.
