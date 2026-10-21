# Foreign Freelance Profile (Thailand Account & Tandem Delivery)

> **Strategy & Legal Setup:**  
> Account Holder & KYC: Thai Citizen residing in Bangkok, Thailand (Bangkok Bank / Kasikorn Bank account for direct USD/THB payouts via Stripe, Wise, or direct bank transfer).  
> Technical Lead & Engineering: Mikhail Kolesnikov (15+ years IT experience, Senior Python & AI Automation Engineer).  
> Dual-Persona Architecture: Transparent agency/studio presentation ensuring compliance with platform terms (Contra & Freelancer.com) and seamless Zoom/client communications without identity mismatch concerns.

---

## 1. Profile Identity & Tagline

### Display Name Options
* **Studio / Agency Name (Recommended for Contra & Freelancer):**  
  `Siam Automation Studio` *(or `Bangkok Automations`)*
* **Tandem / Personal Name (Direct pairing):**  
  `[Partner First Name] S. & Mikhail K. | Automation Studio`

### Professional Taglines / Headlines (Choose by platform)
* **Tagline 1 (Scraping & Automation focused):**  
  `Python Automation, Web Scraping & AI Agents | Bangkok Team`
* **Tagline 2 (General IT & Bots focused):**  
  `Custom Telegram Bots, Data Scraping & LLM Workflows (Python / FastAPI)`
* **Tagline 3 (Short for Freelancer.com headline - 50 chars):**  
  `Python Automation, Web Scraping & AI Agents`

---

## 2. Bio / Summary (Under 1,000 characters)

```text
We are a Bangkok-based automation duo: local operations and account management backed by Mikhail, a Senior Python & AI Automation Engineer with 15+ years of IT experience.

WHAT WE BUILD & DELIVER FAST (1–3 DAYS):
• Web Scraping & Data Extraction: Robust Python scrapers (Playwright, BeautifulSoup, undetected-chromedriver, aiohttp) with proxy rotation, anti-bot bypass, and clean CSV/Excel/PostgreSQL exports.
• Chatbots & Community Tools: Custom Telegram & Discord bots (aiogram, Telethon, discord.py) with webhook queues, FSM state machines, and payment integrations.
• AI & LLM Automation: Production RAG pipelines, OpenAI / Anthropic / Gemini integrations, automated audio transcription (Whisper), and document summarization.
• Backends & Workflows: Lightweight FastAPI microservices, n8n / Make integration workflows, and Dockerized Linux deployments.

Transparent communication, clean tested code, and post-delivery support. Based in Bangkok (UTC+7, overlapping with EU, US, and APAC hours).
```
*(Character count: ~930 characters with spaces)*

---

## 3. Top 15 Skills & Hourly Rates

### Top 15 Categorized Skills for Profile Setup
1. **Python**
2. **Web Scraping**
3. **Data Extraction**
4. **Playwright**
5. **Telegram Bot API**
6. **aiogram**
7. **FastAPI**
8. **API Integration**
9. **Automation**
10. **AI Agents**
11. **OpenAI API**
12. **ETL Pipelines**
13. **Data Parsing (CSV / JSON / Excel)**
14. **n8n / Workflow Automation**
15. **Docker & Linux**

### Rates Strategy
* **Freelancer.com Starting Rate:** `$25.00 / hr` (optimal to gain first 5-star reviews rapidly while staying competitive for US/EU clients).
* **Contra Starting Rate:** `$35.00 – $45.00 / hr` (or project packages starting from `$75` for small scrapers / bots).

---

## 4. Zoom / Meeting Communication Script (Call Transparency)

When a client requests a video/audio call:
> "Hello! Our operations and account management are hosted by [Partner Name] here in Bangkok, and Mikhail leads technical architecture and live engineering. Mikhail will be joining the call to discuss the exact data structures, API endpoints, and screen-share the live demo."

---

## 5. Ready-to-Send Proposal for First Trial Project

* **Target Project:** [Gas Safe Register Website Data Extraction](https://www.freelancer.com/projects/data-extraction/gas-safe-register-website-data)
* **Platform:** Freelancer.com (`gig_73f3d2da3ed55b02`)
* **Client Budget:** `$30.00 – $250.00 USD`
* **Task Summary:** Extract all Gas Safe registered businesses across England (~45 remaining postcode areas or complete re-run) into a deduplicated CSV, overcoming the 50-result search limit.

### Proposal Pitch (Customized to all client requirements):
```text
Hello!

We can extract the remaining ~45 postcode areas (or the entire England directory) into a clean, deduplicated CSV matching your exact field specifications.

Here is our direct answer to your 4 questions:

1. Handling the 50-result cap:
The Gas Safe Register search caps out at 50 results per query in high-density areas. Rather than searching only at the outward postcode district level (e.g. "GU1"), our Python scraper recursively walks down to the postcode sector level (e.g. "GU1 1", "GU1 2", "GU1 3") and sub-grid coordinates. If any sector query approaches 50, the script automatically partitions the search radius, ensuring zero businesses are dropped.

2. Unique de-duplication:
All records will be stored in an internal SQLite database keyed on Gas Safe Registration Number as the primary key. Duplicates across overlapping postcode searches are merged in real time.

3. Deliverables:
• Clean, fully deduplicated CSV with all 8 requested fields (Registration #, Business Name, Address & Postcode, Phone, Email where present, # Engineers, Sole Trader / Ltd, Region).
• Postcode area summary report (counts per postcode area for instant coverage verification).
• Full clean Python script (Playwright/aiohttp) with documentation so you can re-run it anytime.

4. Test Area, Price & Timescale:
• Paid Test on GU (dense area): $35 USD, delivered within 3–4 hours of approval.
• Full Job (all remaining 45 postcode areas or full England re-run): $130 USD fixed, delivered within 24 hours.

We are ready to start the GU test extraction immediately. Looking forward to working together!
```
