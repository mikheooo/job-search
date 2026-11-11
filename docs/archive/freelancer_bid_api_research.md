# Freelancer bid API: P8 independent research

Evidence dates: authenticated developer dashboard observed in BrowserOS neo on 2026-10-05; official terms and API Terms rechecked through Agent Reach Exa on 2026-10-07. Research agent: independent P8 QA. Primary sources only. The official `freelancer/freelancer-sdk-python` repository was read through GitHub CLI. No token, cookie, authenticated API response, or real submission was used. BrowserOS neo was offline on 2026-10-07, so the account dashboard was not rechecked that day.

## Verdict and policy gate

**An official programmable bid API exists. Its existence does not establish permission for this account's unattended auto-bidding.** Current [User Agreement](https://www.freelancer.com/about/terms), last modified 05 June 2025, section 33 requires express written permission for automated access and explicitly includes API access. The decisive short excerpt is: **“without our express written permission.”** Section 4 also prohibits spam and bulk unsolicited communications.

[API Terms](https://www.freelancer.com/about/apiterms), last modified 21 December 2017, incorporate the User Agreement. Section 5.1 says cached data should be refreshed at least every 24 hours and describes this as a limited cache permission for performance; section 5.2 requires strong encryption; section 5.3 says data may not be copied or stored, including information expressed by it such as hashes or transfers, except to the extent permitted by those terms. The integration license does not explicitly waive section 33 for this particular autonomous bidding application. The reviewed text does not establish that P8 may persist its API-derived tracker fields or how long it may retain them. This is a conservative operational interpretation of the two primary texts, not a claim that all API bidding is permanently prohibited.

Consequently:

- `AUTO_APPLY`: disabled until documented Freelancer permission covering this application, bidding behavior, account, and the specific API-derived data fields/retention period is verified, alongside OAuth scopes and account eligibility.
- `APPROVAL_REQUIRED`: can prepare/review a bid locally. A human approve does **not** waive provider permission or account requirements; the programmatic submit remains blocked until these are satisfied.
- `AUTOMATIC_SUBMISSION_BLOCKED_BY_PROVIDER / WRITTEN_PERMISSION_REQUIRED`: describes the documented conditional requirement when written permission has not been established. It must not be presented as an API endpoint being unavailable or an absolute provider ban.
- Missing OAuth authorization is a **separate** `MISSING_ACCESS` blocker, not proof of a provider restriction.

The [developer contact](https://www.freelancer.com/contact-enterprise-api-sales) is linked from the official developer site. Ask Freelancer to confirm automated, individually grounded bids through the documented API for this account/client; clarify whether OAuth client registration grants the section-33 permission; and confirm whether P8 may retain project identifiers/URLs/titles, bid counts, provider bid/response IDs, proposal and description hashes, submitted price/time, and monitoring status, for what duration, and under what encryption. No support message was sent during this research.

## Official operations

Production base: `https://www.freelancer.com/api`. Current [Bids reference](https://developers.freelancer.com/docs/projects/bids) documents every operation below. All listed bid operations require OAuth scopes **`basic` and `fln:project_manage`**.

| Operation | Official method/path | Evidence / important limits |
| --- | --- | --- |
| Place bid | `POST /projects/0.1/bids/` | Create-a-Bid reference and [bidding use case](https://developers.freelancer.com/docs/use-cases/bidding-on-a-project#header-creating-a-bid). JSON body. |
| Update bid | `PUT /projects/0.1/bids/{bid_id}/` | Update-a-Bid reference explicitly lists `description`, `amount`, `milestone_percentage`. Do not infer a supported `period` update from the create payload. |
| Read own bid | `GET /projects/0.1/bids/{bid_id}/` | Get-Bid-by-ID reference. Check returned `bidder_id` against authenticated self; a successful GET alone does not prove ownership. |
| List own active bids | `GET /projects/0.1/bids/` | Documented filters `bidders[]=self_id`, `frontend_bid_statuses[]=active`; `projects[]`, `bids[]`, `limit`, `offset` are also documented. Paginate; do not mistake a first page for exhaustive absence. |
| List own bids for reconciliation | `GET /projects/0.1/bids/?bidders[]=self_id&projects[]=project_id` | Do not filter to active only when recovering an uncertain POST; retracted/awarded bids still establish prior submission. |

The read/list endpoint has documented `award_statuses[]` choices `awarded/rejected/revoked/pending/canceled`, `complete_statuses[]`, `paid_statuses[]`, and `project_details` projection. These support read-only monitoring. Bid actions (accept/retract/highlight/sponsor/award/etc.) are distinct writes and are outside P8's monitoring authorization.

## API data retention and P8 tracker

The API Terms storage clause is material to the P8 design: a bid tracker is not automatically exempt merely because it belongs to the account owner or uses encryption. P8's SQLite schema retains project ID/URL/title, bid counts, provider bid and response IDs, bid price/delivery, timestamps, proposal hash, and project-description hash. The Terms' §5.1 performance-cache permission and §5.3 storage restriction do not establish that these fields may be retained for P8's operational tracking or how long. No API-derived row was written to the live tracker during this research.

The submission policy therefore separately requires `api_data_retention_permitted: true` and a `retention_permission_reference`, in addition to strong-encryption evidence. The reference must substantiate permission for the proposed fields and retention period; a general API integration license, OAuth consent, disk encryption, or one-bid approval alone is insufficient. Until Freelancer clarifies the permitted data/retention scope in writing, all automated API reads and writes remain blocked. This is an operational safeguard, not a definitive legal interpretation of what the broad exception in §5.3 permits.

The 2026-10-07 code update closes direct P6 scraper/API and P8 transport paths before network access when written permission is absent. P8's CLI now selects an in-memory SQLite database persisted only as a DPAPI-encrypted image; it rejects plaintext database files. The policy requires a numeric retention value from 1 to 86,400 seconds, and the encrypted tracker expires and purges API-derived rows/outbox/monitor data when reopened after that deadline. Bid POSTs additionally require the engine's one-use permit tied to the approved payload hash. These are local guardrails, not provider permission: no live request or bid was made during this update. Earlier P6/P7 plaintext snapshots are preserved and remain a separate data-disposition concern.

## Exact create body

The official use case requires:

```json
{
  "project_id": 123,
  "bidder_id": 456,
  "amount": 30000,
  "period": 3,
  "milestone_percentage": 100,
  "description": "The reviewed project-specific proposal"
}
```

This is an illustrative schema, not an actual account/project bid. `bidder_id` must come from authenticated self, never a fabricated placeholder on a real POST. `period` is delivery days. **Currency is the project's currency, not an independently supplied create-body field.** Validate the pack currency against the latest project response. Description is optional at provider level but mandatory for this engine. `milestone_percentage` creates the initial percentage milestone if awarded; a multi-milestone list is not documented as part of this body. Never silently post additional monetary/milestone endpoints or pretend a local milestone list was sent.

The use case's documented successful response has `status=success`, `result.id`, `result.project_id`, `result.bidder_id`, `result.amount`, `result.period`, `result.description`, `result.submitdate/time_submitted`, and `request_id`. Re-read own bid and compare identity/price/days/proposal before marking confirmed. These are documented response examples, **not observed live submissions**.

Official SDK snapshot: commit [`17b8969d7480f3b9ea38d32e499e6c9bb3dd28b8`](https://github.com/freelancer/freelancer-sdk-python/tree/17b8969d7480f3b9ea38d32e499e6c9bb3dd28b8). [`place_project_bid` / `get_bids`](https://github.com/freelancer/freelancer-sdk-python/blob/17b8969d7480f3b9ea38d32e499e6c9bb3dd28b8/freelancersdk/resources/projects/projects.py) and [place-bid example](https://github.com/freelancer/freelancer-sdk-python/blob/17b8969d7480f3b9ea38d32e499e6c9bb3dd28b8/examples/place_project_bid.py) agree with the create body. The older SDK lacks all current list filters; current developer reference, not invented SDK methods, supplies `bidders[]` and active status filters.

## Authentication and permissions

[SDK Session](https://github.com/freelancer/freelancer-sdk-python/blob/17b8969d7480f3b9ea38d32e499e6c9bb3dd28b8/freelancersdk/session.py) requires OAuth and sends `Freelancer-OAuth-V1`. The official example loads `FLN_OAUTH_TOKEN` from the environment. [Advanced scopes](https://developers.freelancer.com/docs/authentication/advanced-scopes) explains user-granted application permissions: `fln:project_manage` covers projects/bids/milestones; `fln:user_information` covers user/profile information; `fln:messaging` covers reading **and sending** messages. P8 must never send messages automatically, even with that scope.

Authenticated self is `GET /users/0.1/self/`, as supported by [official SDK users](https://github.com/freelancer/freelancer-sdk-python/blob/17b8969d7480f3b9ea38d32e499e6c9bb3dd28b8/freelancersdk/resources/users/users.py). The [official SDK detail helper](https://github.com/freelancer/freelancer-sdk-python/blob/17b8969d7480f3b9ea38d32e499e6c9bb3dd28b8/freelancersdk/resources/users/helpers.py) supplies documented `jobs=true`, `status=true`, `balance_details=true`, `profile_description=true`, `display_info=true`, and `avatar=true` query projections; `get_self` accepts that helper result. Requesting a projection does not establish an observed readiness value. Successful self with a numeric ID establishes authentication; scope grants and written permission remain separate evidence. OAuth credentials must stay in environment/secret storage, never docs, payload exports, SQLite, exceptions, or command arguments.

No personal-token/client-generation instructions are asserted as verified here: attempts to read those additional Neo pages timed out after the verified sources above. The user's initial authorization must follow the official account developer settings, not captured browser credentials.

## API request limits versus bid quota

[Rate Limiting reference](https://developers.freelancer.com/docs/api-overview/rate-limiting) states that each endpoint has limits; exceeded limits return HTTP 429 with `AuthorisationExceptionCodes.RATE_LIMITED`. Inspect `RateLimit-Limit` and `RateLimit-Remaining`. Its 50/60-seconds and 1000/3600-seconds values are **examples**, not a verified create-bid allowance. Bid-specific numeric API limits remain **UNKNOWN** until actual authenticated headers/provider confirmation. P8's local 2/hour and 8/day are additional safeguards, not replacements for provider limits. Do not automatically retry an uncertain POST on 429, timeout, 5xx, or a lost response; reconcile first.

[Official bidding FAQ](https://www.freelancer.com/faq/topic.php?id=5) describes membership bid quotas, including free membership 6/month and periodic replenishment. This does not reveal this account's current remaining quota. A successful public project lookup does not reveal balance, quota, profile or account restrictions.

## Account eligibility: verified requirements, unknown account state

The [official bidding use case](https://developers.freelancer.com/docs/use-cases/bidding-on-a-project#header-adding-job-ids) states **all project-required skills** must be on the freelancer account; the FAQ describes at least one. Preserve the discrepancy and use the stricter documented API path until Freelancer clarifies. This is a platform profile skill requirement, separate from internal proven capability. Do not add skills to the account automatically.

[Bidding FAQ](https://www.freelancer.com/faq/topic.php?id=5) lists profile completion (picture, summary, professional headline), exhausted bid quota, missing skills, account restrictions, low-quality-bid restriction, and Escrow.com signup for a Californian client as blockers. [User Agreement](https://www.freelancer.com/about/terms), section 6, requires the available wallet balance to meet the applicable minimum and allows project/account-specific increases. [Fees and Charges](https://www.freelancer.com/feesandcharges) supplies the published base requirement; it is not evidence of this account's balance or its applicable threshold. No automatic deposits or Escrow signup are authorized by the bid test.

| Readiness field | Evidence needed | Current research status |
| --- | --- | --- |
| authenticated | Successful official self response with numeric ID | UNKNOWN; no authenticated request made |
| email_verified | Explicit authoritative account value | UNKNOWN; no verified schema/response |
| profile_complete | Explicit value or independently verified complete required profile | UNKNOWN |
| required_skill_present | Official account jobs compared with latest project jobs | UNKNOWN |
| bid_quota_available | Explicit remaining quota/replenishment state | UNKNOWN; membership alone insufficient |
| balance_requirement_met | Available funds and applicable project/account threshold | UNKNOWN; base threshold alone insufficient |
| account_restricted | Explicit restriction/sanction/limited-account state and other bid blockers | UNKNOWN; missing fields cannot mean unrestricted |

The Bids reference documents user projections such as `user_jobs`, `user_balance_details`, `user_financial_details`, `user_status`, `sanction_details`, and `limited_account`. These projections are not proof of a specific readiness-result field or completeness. The authenticated-user documentation navigation timed out in Neo; no undocumented response keys for quota/email/profile/minimum balance are asserted here. Keep absent or ambiguous values `UNKNOWN` and block submission. An empty list from `compact=true` can omit null fields and must not turn UNKNOWN into PASS.

## Audit boundary

No private endpoint, DOM submission, cookie/session capture, real bid, account change, deposit, message, or provider-permission request was performed. Current evidence establishes official endpoint support and a **conditional written-permission requirement**; it does not establish the account has those rights. The first real E2E is pending until these facts are independently verified. Local/mock success cannot satisfy Stage C.

### Live account developer-access observation

The coordinating agent observed the [official developer account dashboard](https://accounts.freelancer.com/settings/develop) on 2026-10-05 after the user signed in: it displayed **“Please verify your payment and email first.”** It exposed no available client-creation form. This is an account-specific initial developer-access blocker, not evidence that the public bid API is absent or universally prohibited. Browser login was observed; API authentication and granted scopes remain unverified. The message did not reveal which individual verification is missing, wallet balance, bid quota, profile completeness, or account restrictions. On 2026-10-07 BrowserOS neo was offline, so current account status is `UNKNOWN`; do not treat the 10/5 observation as current. No deposit, identity/payment verification, or token capture was performed by agents.

Although the advanced-scope guide says `fln:messaging` can read messages, the current QA evidence did not establish an official conversation/thread read operation or an account entitlement. The monitor therefore records client reply as `UNKNOWN`; it does not inspect messages or send replies. This remains an extension point after API permission, entitlement, and endpoint verification.
