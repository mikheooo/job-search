# T5.1 — Master resume content review

Status: **implemented and measured**. T5 built the generator; T5.1 is a review of
what the generator actually printed. It changes no knowledge-base fact, does not
touch `resume.md`, `candidate_profile.json`, `state.db`, the filters or the
application pipeline, and does not start T6–T8.

Line references are in the `grep -n` coordinate system.

---

## 1. What the review found

Five things were wrong or unproven in the T5 output. Four were fixed in the
generator; one is a wording decision that only the candidate can make and is now a
declared proposal rather than a silent choice.

| # | what was wrong | where it showed | fix |
| --- | --- | --- | --- |
| 1 | One company's confirmed dates were attached to four employers | the chronology note named all four employers before `March 2018 – March 2021` | the period is now attributed to `Speedcast / Germsat` alone |
| 2 | An artificial 2018–2021 gap | the block said `2009 – 2018`, Avantis starts April 2021, and the note did not fill the space | the note now fills it: the timeline reads 2009–2018, Speedcast to March 2021, Avantis from April 2021 |
| 3 | The document sold one direction | the headline was `IT Support & Application Support \| Bitrix24 / CRM Automation \| Python & AI Automation` and the summary opened `AI Automation Engineer` | the headline covers four directions in alphabetical order; the summary's single-direction opening is omitted |
| 4 | Three tenure numbers, none approved | `15+ years` in the resume, `11.0` in the profile, `3` in `years_experience` | no figure is published; a formulation is proposed with its arithmetic shown |
| 5 | A block line glued to the bullet above it | the project `**Stack:**` line followed a bullet with no blank line between them, in both languages | `render_markdown` now treats `meta` as a block kind when inserting the leading blank line |

Finding 5 is a rendering defect rather than a content one, and it was found by
reading the printed drafts, not the code. It had no test at all: `project_stack`
appeared in no test and in no mutation, which is why 58 tests and 18 mutations all
passed while one line of the document rendered wrong. `render_markdown` inserted a
blank line *before* `h2/h3/paragraph/note` but only *after* `meta`; a role's meta
line is rescued by the `h3` above it, but a project's `**Stack:**` line is not, so
markdown read it as a lazy continuation of the bullet. The fix is one word in the
leading set, a new pin, and a new mutation (§8).

Plus one thing that was right and is now pinned: no client, no revenue and no
metric is claimed for any project, and no short break is explained anywhere.

---

## 2. The chronology, corrected

The confirmed claim is:

> Speedcast/Germsat: период работы март 2018 — март 2021 подтверждён. Учесть при
> восстановлении хронологии, не считать эти годы перерывом.

Three facts follow from it, and the T5 output honoured only the first:

1. the period belongs to **Speedcast/Germsat**;
2. it belongs to **that company alone** — the candidate confirmed nothing about
   Intravision, Slavyanka or Russian Post;
3. those years are **not a gap**, so the document must not imply one.

The role's own heading is one block naming four employers, so `role.company` is the
combined string. Rendering `*{role.company}: March 2018 – March 2021*` therefore
dated three companies by a fact nobody confirmed about them — that was the bug.

`CHRONOLOGY_EMPLOYER` now names the employer the period belongs to, and the rendered
note is:

> *Speedcast / Germsat: March 2018 – March 2021 (period confirmed by the candidate).*

Two guards hold it:

* `test_the_confirmed_period_names_an_employer_of_that_role` — the name must be one
  of the role's own `role_employer` claims. `Speedcast / Germsat` is
  `role_employer-ab93224052`; a name the source never uses fails.
* `test_the_confirmed_period_is_attached_to_one_employer_only` — no rendered line
  may contain `March 2018` or `March 2021` without naming Speedcast/Germsat, and no
  such line may name any of the other three.

### Why the block keeps `2009 – 2018`

The block's dates are the resume's own claims (`role_start-6da0482626` = `2009`,
`role_end-27a202567d` = `2018`). Rewriting them to reach March 2021 would be this
generator inventing a date, and inventing a date for Intravision, Slavyanka and
Russian Post is exactly what the review forbids.

Keeping them and stating the confirmed company's period beside them produces a
**continuous** timeline, which is what the candidate asked for:

| | period |
| --- | --- |
| Earlier block (four employers) | 2009 – 2018 |
| Speedcast / Germsat, confirmed | March 2018 – March 2021 |
| Avantis | April 2021 – August 2022 |
| Sayama Travel | November 2022 – April 2026 |
| Own projects | May 2026 – present |

`test_the_early_block_keeps_the_sources_own_dates` pins that the block still prints
`2009` and `2018` and that the end date is not silently rewritten.

### What was removed

The old note ended with *"individual dates and job titles for the other employers in
this block are not recorded in any source and are deliberately left out"*. That
sentence is gone. A resume does not explain its own missing data, and a reader who
was not looking for a gap was being told to look for one. The reasoning lives here
and in the source map instead.

---

## 3. Positioning, made universal

The headline was three directions with the AI framing last but the summary opening
with `AI Automation Engineer` — so the document's first substantive sentence sold
one identity, whatever the headline said.

**New headline** (`POSITIONING_LABELS`, a proposal, marked as one):

```
Application Support & IT Support | Bitrix24 / CRM Automation |
Python Automation & AI Projects | Systems & Network Administration
```

Two deliberate properties, both pinned by
`test_the_headline_covers_every_direction_and_ranks_none`:

* **the set is universal** — support, systems administration, Bitrix24/CRM and
  automation/AI are all present, which is what the candidate's search spans;
* **the order carries no priority** — it is alphabetical, which is the only order
  that ranks nothing. No direction is made the main one for every vacancy, and a
  per-vacancy version (T6/T7) will choose its own lead from this set rather than
  inheriting one.

**The summary.** The quoted text from `resume.md` opened
`AI Automation Engineer with 15+ years of IT experience: …`. That span is now a
declared omission in `OMITTED_SPANS`, with the reason recorded. The summary now
opens:

> API integration, workflow automation, and LLM-powered systems in production.
> Builds end-to-end systems combining Python, LLMs (OpenAI, Gemini, Anthropic),
> REST/webhook integrations, Telegram bots, n8n/Make workflows, and Linux/Docker
> infrastructure. English B1, fully remote, ICT (+7) timezone.

The candidate's own wording is untouched in the KB — the generator deletes a span
from a quote rather than rewriting it, and refuses to run if the span is no longer
there to delete. `test_the_summary_does_not_claim_a_single_direction` and
`test_the_summary_states_no_tenure_number` pin the result, and mutation
`summary_identity_restored` confirms they can fail.

### One gap the review found and did not close

The headline is universal, but **the skills block is not**. All 32 `skill` claims in
the KB come from three groups in `resume.md` — `AI & LLM`, `Automation & delivery`,
`Python & integrations` — and **not one of the 32 names a support or systems
technology**. Measured, not eyeballed:

```
skill groups in the KB: ['AI & LLM', 'Automation & delivery', 'Python & integrations']
skill claims naming support/systems technology: (none)
total skill claims: 32
duty claims naming Active Directory: 1  (duty-39d5320cd4, in the 2009–2018 block)
```

So Windows Server, Active Directory, Exchange, Hyper-V, networking, IP telephony,
ITSM and **Bitrix24** appear in the draft only inside duties and inside the
headline. A recruiter hiring for IT Support or a Bitrix24 administrator scans
`## CORE SKILLS` and finds none of them — even though Bitrix24/CRM is one of the
four directions the headline now advertises.

This is left open on purpose. The generator quotes skill groups; it cannot invent
one, and a `IT Support & Systems` group would be **new prose about the candidate**,
which is a proposal and needs approval — exactly like the summary opening. Adding it
silently would be the same defect as publishing an unapproved tenure figure. It is
reported here rather than added to `PROPOSALS` because the three items there were
already under review when this was found; it is offered as a fourth decision.

---

## 4. Tenure: three numbers, none published

| source | number | what it actually measures |
| --- | --- | --- |
| `resume.md` summary | "15+ years of IT experience" | the candidate's own claim about the whole IT career |
| `candidate_profile.json` | `domain_years.it_support = 11.0` | years in the **support domain**, a subset |
| `candidate_profile.json` | `years_experience = 3` | a third field; **not** total tenure |

The T3 rule `total_experience_vs_it_support` flagged the first against the second.
The review's position, and the reason the rule's reading is too strong: those two
numbers measure different things and do not contradict each other automatically.
`years_experience = 3` is not total tenure either, and treating it as such was the
other half of the error.

**Is "15+ years" supported?** By the resume's own timeline, yes: the earlier block
starts in `2009` and the last employed role ends April 2026 — about 17 years, all of
it IT. So the figure is defensible, and the proposal in the source map says so and
shows the arithmetic.

**But no figure is published.** The number is a claim about the candidate, the
wording needs approval, and publishing it silently is precisely the failure T5's
omission mechanism exists to prevent. `15+ years of IT experience` appears nowhere in
either draft.

---

## 5. Projects: implemented, prototype, plan

The label printed after each project's name comes from `PROJECT_CATALOGUE`
(`ProjectSpec.status`). It is the **catalogue's reading** of the project's state, not
a sentence the project wrote about itself — and the first draft of this review got
that wrong, calling them "the repositories' own words anchored to a line". They are
not, for three of the four projects:

| project | label | anchored in the repository? |
| --- | --- | --- |
| Job Search AI | Implemented | **no** — declared in the catalogue |
| Reels Analyzer | Implemented | **no** — declared in the catalogue |
| Repiton | **Prototype** | **yes** — two `project_status` anchors, `STAGE_1_FINAL_NO_GO` and `TRACK B … BLOCKED` |
| WHUBN | Implemented | **no** — declared in the catalogue |

That distinction is now declared rather than left to the reader:
`STATUS_WITHOUT_ANCHOR` names the three projects whose label is a judgement, each
with the reason, and
`test_every_project_status_is_either_anchored_or_declared` holds the partition —
every project must be anchored *or* declared, never both and never neither, so a
project added later cannot pass unnoticed. Mutation
`status_anchor_claim_overstated` confirms the test can fail.

The plan-versus-built distinction is carried inside the bullets rather than by a
label, because that is where the sources draw it: Repiton's rendered bullets include
`STAGE_1_FINAL_NO_GO` and `TRACK B — PRONUNCIATION SCORER VALIDATION: BLOCKED BY
STAGE_1_FINAL_NO_GO`, so the blocked work is visible as blocked. A note in the source
map records the whole position.

**No client, no revenue, no metric.** Nothing in the catalogue can express one: every
project claim is the whitespace-normalised text of a line that is really in the
repository, so there is nowhere for an invented figure to enter.

**"in production".** The phrase is the candidate's own and the systems do run — Reels
Analyzer runs under Docker with PostgreSQL and Redis, Job Search AI is in daily use.
For personal projects with no client it can still read as a claim about a commercial
deployment, so a softer alternative is offered as a proposal rather than substituted
silently.

**Authorship.** Nothing in the draft claims that all the code was written unaided.
The project bullets describe what the systems do; the duties are the candidate's own
statements from `resume.md`. The wording is a proposal item, not a defect.

**Short breaks.** No line explains one. `grep` for gap/break language across both
drafts returns nothing.

---

## 6. Proposals, kept out of the drafts

New prose about the candidate is the candidate's to approve, so it lives in a
`PROPOSALS` table rendered into the source map under
**"Proposals awaiting approval"**, and nowhere else:

| proposal | why it needs a decision |
| --- | --- |
| A neutral opening for the professional summary | new prose; the quoted one frames the document as one direction |
| `15+ years in IT` as the tenure wording | a claim about the candidate; supported by the timeline but needing approval |
| `LLM-powered systems in daily use` instead of `in production` | the softer alternative to the candidate's own phrase |

`test_no_proposal_reaches_either_draft` asserts that not one of them appears in the
English or the Russian draft, and mutation `proposal_published_as_the_summary`
confirms the test can fail.

---

## 7. The source map

`artifacts/master_resume_sources.md` now opens with a **brief map** — claim counts
per origin, computed from the KB rather than written down, so it cannot drift:

| where the content comes from | claims |
| --- | --- |
| `resume.md` (T1 parser) | 96 |
| T4.5 interview | 5 |
| project catalogue (T5) | 21 |
| **total** | **122** = 113 rendered + 5 excluded + 4 subsumed |

`test_every_origin_marker_has_a_label` fails if a new origin appears without a
label, so the brief map cannot quietly lose a source. The full per-claim tables
follow, unchanged: that is the audit trail, and the brief map is the summary of it.

---

## 8. Tests and mutations

`tests/test_candidate_master_resume.py` — **59 tests, 59 passed** (was 48).

Eleven added or strengthened:

| test | what it prevents |
| --- | --- |
| `test_the_confirmed_period_names_an_employer_of_that_role` | the period attributed to a company the source never names |
| `test_the_confirmed_period_is_attached_to_one_employer_only` | one company's dates attached to three others |
| `test_the_early_block_keeps_the_sources_own_dates` | the block's dates silently rewritten to hide a gap |
| `test_the_headline_covers_every_direction_and_ranks_none` | a headline that sells one direction |
| `test_the_summary_states_no_tenure_number` | an unapproved tenure figure published |
| `test_the_summary_does_not_claim_a_single_direction` | the summary opening on one identity |
| `test_no_proposal_reaches_either_draft` | unapproved prose reaching the draft |
| `test_the_tenure_proposal_shows_its_arithmetic` | a tenure figure asserted without its basis |
| `test_the_source_map_carries_the_brief_map_and_the_proposals` | the proposals the review waits on going missing |
| `test_every_project_status_is_either_anchored_or_declared` | a status label the repository never states passing as fact |
| `test_no_block_line_is_glued_to_a_bullet_above_it` | a block line losing its leading blank and rendering as part of the bullet above it |

`test_chronology_evidence_is_confirmed_and_matches_the_period` was extended to check
that the three chronology tables describe the same roles.

`tools/t5_guard_mutation.py` — **19 mutations, 19 caught** (was 11). Eight added:

| mutation | what it breaks |
| --- | --- |
| `period_attached_to_whole_block` | one company's confirmed dates attached to three others |
| `chronology_employer_names_a_stranger` | the period attributed to an invented employer |
| `headline_gets_a_lead` | the headline order implying a priority |
| `summary_identity_restored` | the summary selling one identity again |
| `proposal_published_as_the_summary` | unapproved prose published |
| `proposals_section_dropped` | the proposals vanishing from the map |
| `status_anchor_claim_overstated` | a project that quotes its own status written off as a judgement |
| `meta_line_glued_to_the_bullet_above` | the project `**Stack:**` line losing its leading blank and rendering inside the bullet above it |

Every mutation restored the original bytes, verified by SHA-256.

The 19 mutations were **not** run in one process. Each mutation is one pytest run,
and the sandbox's deletion watcher trips after roughly six runs inside a single
shell call, killing the rest during cleanup — after pytest finishes the test but
before it prints a summary. The first attempt at all 19 in one call returned
`0/19`, every one of them `INVALID` with `NO SUMMARY`; the guard refused to read
those as `SURVIVED`, which is the point of the verdict logic, but it is not a
measurement either. The guard now takes mutation names on the command line and was
run as four batches (1 + 6 + 6 + 6), each in its own shell call: `19/19 CAUGHT`,
each with a real `N failed` summary line.

---

## 9. Regression

Full suite, on the frozen code, after the §1 finding-5 fix:

```
.venv/Scripts/python.exe -m pytest -q --junitxml=.pytest_t51_v2.xml
```

**2293 tests, 1 failure, 0 errors.**

The verdict is read from the junit file, not from the console. The run reached
`[100%]` and then the sandbox's deletion watcher killed pytest during cleanup, so
there is **no summary line** in the log and the process exit code is `1`:

```
............................................................. [100%]
[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":182,"threshold":50,...}
EXIT=1
```

`--junitxml` is written in `pytest_sessionfinish`, which runs once the whole session
has finished — before cleanup. So it survives a kill that lands on cleanup, but a run
killed *mid-session* leaves no file at all. That distinction is the reason the verdict
is read from the file rather than from the exit code: the file being present proves
all 2293 testcases ran, and a missing file would mean "no measurement" rather than
"no failures".

| | |
| --- | --- |
| tests | 2293 |
| failures | 1 |
| errors | 0 |
| `tests/test_candidate_master_resume.py` | **59 testcases, 0 failures** |
| `state.db` before / after | `2f28334839de4d8c` / `2f28334839de4d8c` |

The single failure is **not** T5.1's:

```
FAIL tests/test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches
AssertionError: the report grew new symbol mismatches ... flagged for reading: 24
```

That is the report ratchet, red at 24 against a threshold of 18. It was already red
before T5.1 — the T5 run failed on the same test with the same number — and it is
caused by an uncommitted foreign edit in `cli.py`, not by this work. T5.1 makes the
claim structural rather than circumstantial: the 24 flagged references point into
fourteen files (`hh_submit_policy.py`, `hh_submission.py`, `application_queue.py`,
`hh_message_watcher.py`, `db_schema.py`, `browser_executor.py`,
`hh_application_queue.py`, `application_tracking.py`, `telegram_notifier.py`,
`db.py`, `hh_questionnaire.py`, `cli.py`, `hh_application_runner.py`), and **not one
of them points into `master_resume.py`, `test_candidate_master_resume.py` or
`t5_guard_mutation.py`** — the three files T5.1 changed. `docs/ble001_triage.md`
names none of them, so no edit to them can move that count. The reference gate
itself is green: 301 distinct references, **0 out of range**.

Against the T5 run (2282 tests, 1 failed, 2281 passed) the suite grew by 11 tests,
which is the 48 → 59 growth in §8; the failure count and its identity are unchanged.

Determinism, re-measured after the fix and across two processes: the CLI build and a
separate probe process produce identical documents, and the on-disk artifacts are
byte-identical to the code's output once CRLF is normalised (`write_text` writes
CRLF on Windows, which is why a raw byte hash of a rendered string does not match a
raw byte hash of the file — a mistake made and corrected during this review).

| artifact | sha256 (text, LF) | lines |
| --- | --- | --- |
| `artifacts/master_resume.md` | `c319b30d8e02…` | 106 |
| `artifacts/master_resume.ru.md` | `3e9fd8d68274…` | 106 |
| `artifacts/master_resume_sources.md` | `60addc25ae37…` | 204 |

Protected files, byte-identical after the whole review: `resume.md`
`7ae200cdd7251d3c`, `candidate_profile.json` `d3b9baf7d2443f7a`,
`ai_assistant/candidate_profile.json` `652c96170f581fa0`, `state.db`
`2f28334839de4d8c`, `candidate_kb.json` `fafcedb07c8016e9` (kb_version 27),
`ai_assistant/cli.py` `83806cf0b2a3dee8`. **T5.1 makes no edit to `cli.py`.**

---

## 10. The three clarifications

Everything else the review asked for is done. Three decisions remain, and each one
changes the published document:

1. **The professional summary's opening.** The draft now opens on the capability
   statement, because the quoted opening sold one direction. Proposed replacement:
   *"IT specialist with 15+ years across technical support, systems administration
   and Bitrix24/CRM, now building Python automation and AI systems of my own."*
   Approve it, or supply your own — the generator will not invent one.
2. **Whether to state a tenure figure, and which.** *"15+ years in IT"* is supported
   by the timeline in §4. Say yes and it goes in; say no and the draft keeps no
   number. If you prefer the support-domain figure, say so explicitly, because
   `11.0` and `15+` are not the same claim.
3. **`in production`.** Keep it, or use *"LLM-powered systems in daily use"*. Either
   is defensible; the phrase is the one a recruiter is most likely to probe.

Not asked, and not needed: the individual dates for Intravision, Slavyanka and
Russian Post. They are absent from every source, and the draft states no date for
them. Nothing has to be explained about the years between.

**Optional fourth, offered rather than asked** — the skills-block gap in §3. The
headline now advertises Bitrix24/CRM and Systems & Network Administration, but
`## CORE SKILLS` carries no support or systems technology at all, because none of
the 32 skill claims in the KB is one. Closing it means drafting an
`IT Support & Systems` group from the `2009–2018` duty text — new prose, so a
proposal, and it would not be published without approval either. Say the word and it
becomes the fourth proposal; leave it and the drafts stand as they are.
