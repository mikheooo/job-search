# T5 — Master Resume

Status: **implemented and measured**. This is the first stage whose output a human
outside this repository could read, so it is also the first stage where a wrong
fact would leave the building.

> **Revised by T5.1** — see `docs/t5_1_master_resume_content_review.md`. A review of
> the printed text changed four things this report describes: the chronology note
> (§7), the headline and the summary's opening (§9), a rendering defect in
> `render_markdown` that glued a project's `**Stack:**` line to the bullet above it
> (§12 and §13 both grew as a result), and the list of open questions, which is now
> three rather than eight (§17). The rest of this report stands.

Line references in this file are in the `grep -n` coordinate system.
`master_resume.py`, `projects.py`, `kb.py` and `tests/test_candidate_master_resume.py`
are LF-only, so both coordinate systems agree for them. `cli.py` is not: it carries
6382 bare CRs and 6227 CRLFs, and every edit to it in this task was byte surgery
(§11).

---

## 1. Why this stage exists

T1–T4.5 built a knowledge base of 122 claims, each with a source file, a line and a
quote behind it, and a status saying how much is known about it. None of that is
readable by an employer.

T5 turns the knowledge base into a document. The interesting part is not the
rendering: it is that the document is **checkable**, and that the check is done by
the machine rather than by the candidate's memory.

Three properties carry that:

* every line names the claim it came from, and the source map prints them;
* nothing below `DERIVED_FROM_SOURCE` is rendered, and everything dropped is
  reported with a reason;
* every claim in the knowledge base ends up in exactly one of three buckets, so a
  fact cannot leave the resume with nothing in the output to show it did.

The third one is the one this task had to add. §5 explains why.

---

## 2. What was reused rather than rebuilt

Nothing in T1–T4.5 was re-implemented. Specifically:

| reused | how |
| --- | --- |
| `ClaimRecord` / `ClaimStatus` | unchanged; T5 reads them and never writes a status |
| content-derived `claim_id` | extracted into `make_claim_id()` in `resume_parser.py`, shared with `_IdFactory` |
| `merge_claims` add/refresh/absent semantics | reused for the project sync, with the origin guard extended |
| `evidence["source_file"]` markers | `"resume.md"`, `"interview"`, and the new `"project"` |
| `evidence["superseded_by"]` | reused as the "заменённый факт" marker; T5 excludes claims carrying it |
| the audit's exclusion reasons | reused verbatim as the reason text where they apply |
| `cli.py` dispatch pattern | the same shape as `candidate-kb verify` / `candidate-audit` |

`PROJECT_SOURCE = "project"` is a string in `evidence["source_file"]`, exactly like
`INTERVIEW_SOURCE = "interview"`. It is a marker, not a path, so a claim's origin
survives the repository being moved.

**T3 and T4 were checked, not assumed, to be unaffected** by the 21 new project
claims. `candidate-kb verify` and `candidate-audit --mode all` produce
byte-identical output before and after the sync, once the KB path passed on the
command line is normalised away — the only diff line is `kb_path`. The reason is
structural: T3/T4 select claims by kind (`TECHNOLOGY_BEARING_KINDS`, the
`bitrix_experience_vs_excluded` scan) and the new kinds are `project`,
`project_summary`, `project_status`, `project_stack`, `project_bullet`, none of
which any of them names.

The stronger form of the same fact: **nothing outside T5 imports the T5 modules.**
`kb_verify.py` imports `.kb`, `.resume_parser` and `.candidate_profile`;
`audit.py` imports `.kb` and `.kb_verify`; neither names `master_resume` or
`projects`, and no other module in `ai_assistant/candidate/` does either. The two
`cli.py` insertions sit at lines 4976–4993 and 5478 onwards, past every `cli.py`
line either of them could reach.

---

## 3. Files

**New**

| file | lines | what it is |
| --- | --- | --- |
| `ai_assistant/candidate/master_resume.py` | 1493 | the generator, the exclusion policy and the commands |
| `ai_assistant/candidate/projects.py` | 628 | the project catalogue: 21 anchored quotes across 4 repositories |
| `tests/test_candidate_master_resume.py` | 1236 | 59 tests |
| `tools/t5_guard_mutation.py` | 387 | 19 mutations, each restoring bytes |
| `artifacts/master_resume.md` | 106 | the English draft |
| `artifacts/master_resume.ru.md` | 106 | the Russian review variant |
| `artifacts/master_resume_sources.md` | 204 | the source map |
| `artifacts/candidate_kb.backup_pre_t5.json` | — | the KB as it was before T5 touched it |

**Modified**

| file | change |
| --- | --- |
| `ai_assistant/candidate/kb.py` | `PROJECT_SOURCE`, `is_project_sourced`, `is_document_sourced`, `ProjectSyncReport`, `sync_project_claims` |
| `ai_assistant/candidate/resume_parser.py` | `CLAIM_ID_SEPARATOR`, `make_claim_id`; `_IdFactory.make` now delegates to it |
| `ai_assistant/cli.py` | two subcommands, by byte surgery |

---

## 4. The deliverable

`artifacts/master_resume.md` carries the seven sections the task asked for:
Professional Summary, Core Skills, Employment History, Independent Projects,
Education, Languages, Contact Information. Its length is **not capped** — a cap is
how a fact disappears without anyone noticing, and the task explicitly ruled one
out.

What the master resume is:

* a **universal source**. It is not an AI-Engineer document with the AI direction
  left in as the only framing; §12 lists the per-vacancy versions it is meant to
  feed, and the headline deliberately offers four directions rather than one.
  T5.1 widened it to four and made the order alphabetical; see §9.
* **checkable**. 113 of the 122 claims are printed in the source map with their id,
  status, kind and value, and the remaining 9 are printed in two tables with the
  reason they are not on a line.

What it is not:

* a finished resume. It needs the candidate's approval on the headline, on the
  summary wording and on the questions in §17.
* a generator of per-vacancy versions. That is T6/T7.

---

## 5. The three-way partition

This is the part of T5 that was wrong on the first pass and is worth recording.

The first version rendered every claim it knew how to render and reported every
claim it refused to render. Counting the result gave:

```
total 122 | rendered 101 | excluded 3 | neither 18
```

Eighteen claims were in neither bucket. Two kinds were deliberately *subsumed* —
`experience_statement` restates the summary, `title`/`headline` are folded into the
headline proposal — and eleven were simply **missing**: the independent role's own
facts. Its title, location, start and end were dropped, and so were the role claim
itself and its per-employer splits, because `### Own projects in this period` was
emitted with an empty claim list and only its duties.

That is precisely the failure mode the stage is supposed to prevent: a fact left
the document and nothing in the output said so. The draft still "looked fine".

The fix is a declared three-way partition, with a test that it is exact:

| bucket | meaning | count |
| --- | --- | --- |
| rendered | on a line, with the claim id attached | 113 |
| `exclusions()` | a fact deliberately withheld, with the reason | 5 |
| `subsumed()` | a fact that is present but carried by another line | 4 |
| **accounted** | | **122 of 122** |

`unaccounted_claims()` returns the difference, and `candidate-master build` prints
the balance and exits non-zero when it does not add up:

```
rendered       : 113 claims
excluded       : 5 claims
subsumed       : 4 claims (carried by another line)
accounted      : 122 of 122 claims
```

A second test pins that the three buckets do not overlap. An overlap would mean the
source map credits one fact to two lines, which reads as two facts and inflates the
account until it stops meaning anything.

The overlap was real before it was fixed: a role's heading claims included the
location and the dates, and the `*location | period*` meta line reused the whole
heading set. `meta_claim_ids()` now returns only the claims the meta line actually
renders, and `_Role.heading_claims` no longer carries location or dates.
`test_a_role_heading_and_its_meta_line_share_no_claim` holds that.

### The five exclusions

| claim | kind | reason |
| --- | --- | --- |
| `experience_statement-03e30e1738` | `experience_statement` | equates total IT tenure with tenure in one domain; contradicted by `domain_years.it_support = 11.0` and the T3 rule `total_experience_vs_it_support` |
| `role_company-fcd10805e4` | `role_company` | "Freelance - AI Integration & Automation" names an employer and implies paid client work, which the candidate confirmed did not happen |
| `goal_direction-fa6ff566ec` | `goal_direction` | search strategy, not resume content |
| `preference_role-1f18d3dc89` | `preference_role` | search strategy, not resume content |
| `preference_condition-3d50e70c67` | `preference_condition` | salary expectation: excluded from the public resume on purpose |

### The four subsumptions

`headline-e0d7b28fbb`, `title-931a5ec847`, `title-a2e497e830`, `title-61c18ca65c` —
the candidate's own three target titles and the headline built from them. The resume
headline is assembled as a **proposal** (`POSITIONING_LABELS`) rather than quoted, so
the underlying titles are represented rather than printed. They stay in the KB as
raw material for per-vacancy versions, and the reason says so.

---

## 6. Sources and statuses

```
claims 122
status            {'DERIVED_FROM_SOURCE': 117, 'CONFIRMED_BY_USER': 5}
source marker     {'resume.md': 96, 'interview': 5, 'project': 21}
in_source False   0
superseded        0
kb_version        27
```

The 21 project claims all carry `source_file = "project"`, and each one's evidence
names a repository-relative path, a line number, the quoted line and the SHA-256 of
the file it was read from. `validate_catalogue()` re-reads every quote and raises
`ProjectSourceError` if a quote is not at its line — so a claim cannot survive the
source moving out from under it.

`sync_project_claims()` adds, refreshes and marks absent; it **never deletes and
never changes a status**. A project claim arrives `DERIVED_FROM_SOURCE` whatever
status is handed in, because a catalogue claim is read from a file and is not
confirmed by anybody. A quote that changes mints a new `claim_id`; the old claim is
marked `in_source = False` rather than left bound to the project, which would print
the same fact twice.

---

## 7. Employment history

All six employers the task named are present, and no title or date was invented:

| role | period | source |
| --- | --- | --- |
| AI Automation Engineer — Sayama Travel | November 2022 – April 2026 | `resume.md` |
| Bitrix24 Technical Support Specialist — Avantis | April 2021 – August 2022 | `resume.md` |
| Earlier IT Infrastructure & Support Experience — Speedcast / Germsat; Intravision; Slavyanka; Russian Post | 2009 – 2018 (block), Speedcast/Germsat corrected to March 2018 – March 2021 | `resume.md` + confirmed interview fact |

The last row is where the source contradicts itself, and T5 does not paper over it.
The resume's own block says 2009–2018 and covers four employers in one heading;
Avantis starts April 2021. That leaves 2018–2021 unaccounted for in the source
document. The candidate confirmed Speedcast/Germsat was **March 2018 – March 2021**,
which closes exactly that gap.

The generator therefore renders the block as the source stated it, and adds a note
naming the confirmed period, with the note's own claim id attached:

> *Speedcast / Germsat: March 2018 – March 2021 (period confirmed by the candidate).*

**T5.1 corrected this.** T5's first version rendered the note against the role's
combined employer string, which dated Intravision, Slavyanka and Russian Post by a
fact confirmed about Speedcast/Germsat alone. `CHRONOLOGY_EMPLOYER` now names the
employer, and the block keeps its own `2009 – 2018` rather than being stretched to
2021 — the confirmed period fills the years in between, so the timeline is
continuous and no date is invented. See `docs/t5_1_master_resume_content_review.md`
§2.

The period in that sentence is checked against `CHRONOLOGY_PERIOD`, and
`test_chronology_evidence_is_confirmed_and_matches_the_period` verifies that the
claim it comes from is `CONFIRMED_BY_USER` and really says March 2018 – March 2021
in Russian. The two cannot drift apart without a test noticing.

**Total IT tenure is not stated anywhere.** `candidate_profile.json` says
`years_experience = 3`, `domain_years.it_support = 11.0`, and the resume says
"15+ years of IT experience". Three numbers, three different meanings, and T5 prints
none of them. The phrase in the summary is deleted, not reworded:

`OMITTED_SPANS` removes `" with 15+ years of IT experience"` and **raises** if the
span is no longer there to delete. The failure mode it guards against is the
opposite of the obvious one: if the resume is ever edited, the omission silently
stops applying and the phrase returns, and nothing in the output would show it.
`test_the_omitted_span_is_still_present_to_be_omitted` and mutation
`omission_is_silent` both pin that.

**T5.1 revised this.** The T3 rule reads "15+ years" as contradicted by
`domain_years.it_support = 11.0`, but the two measure different things — the whole IT
career against one domain — so they do not contradict each other automatically, and
`years_experience = 3` is a third number that is not total tenure. The span deleted
is now the whole opening `"AI Automation Engineer with 15+ years of IT experience: "`,
which removes a single-direction identity and an unapproved figure in one declared
step. A `15+ years in IT` formulation is proposed with its arithmetic shown, not
published. See `docs/t5_1_master_resume_content_review.md` §3 and §4.

---

## 8. Independent projects

Since May 2026 the candidate did only their own projects, with no paid orders. The
task asked for that period to be separated from company work and for implemented
work, prototypes and plans to be distinguished. Both are done, from sources that
actually exist:

| project | status | anchors | repository |
| --- | --- | --- | --- |
| Job Search AI (Job Search Hub) | IMPLEMENTED | 5 | `Documents/job-search` |
| Reels Analyzer | IMPLEMENTED | 5 | `reels_bot` |
| Repiton | PROTOTYPE | 6 | `Documents/Repiton` |
| WHUBN | IMPLEMENTED | 5 | `Documents/WHUBN` |

Every project bullet is the whitespace-normalised text of a line that is really in
the repository — a README heading, a status line, a stack line. The catalogue is a
list of `Anchor(rel_path, line, quote, kind)`, and nothing else. There is no free
text in it, so there is nowhere for a client, a revenue figure or a metric to be
invented even by accident.

The period itself is presented honestly rather than as an employer. The role's own
facts — title, location, dates — are rendered, because they are real. The employer
name is not: "Freelance - AI Integration & Automation" would claim paid client work
the candidate denied. And the heading is followed by the confirmed statement:

> No paid client work in this period (confirmed by the candidate).

That sentence carries the claim id of the confirmed interview fact behind it, so it
is traceable rather than being an assertion this module makes on its own.

The AI-assisted-development honesty the task asked for is carried by the sources
themselves rather than by a disclaimer T5 wrote. Repiton's rendered bullets include
`STAGE_1_FINAL_NO_GO` and `BLOCKED BY STAGE_1_FINAL_NO_GO`; Job Search AI's include
the review gate and the opt-in kill-switches; Reels Analyzer's include the Exa
fact-checking with QA controls. The prototype is labelled a prototype and its NO-GO
result is in the document. Nothing claims independent code authorship, because
nothing in the catalogue says who wrote what.

---

## 9. Declared presentation decisions

Four things the generator does are *not* reading the knowledge base. Each is
declared in code, reported in the source map under "Declared presentation
decisions", and needs the candidate's approval:

1. **the headline** — `POSITIONING_LABELS`. T5.1 widened this to four directions
   (support, systems administration, Bitrix24/CRM, automation/AI), taken from the
   candidate's own role lists and the confirmed Bitrix24 preference, and ordered
   alphabetically so that no direction is ranked as the lead;
2. **the own-projects period** — the "no paid client work" line and the move of the
   former "Freelance" heading out of employment, both driven by the confirmed
   interview fact named in `INDEPENDENT_ROLE_EVIDENCE`;
3. **omitted spans** — `OMITTED_SPANS`, with the reason and the rule that found the
   problem (§7);
4. **excluded and subsumed claims** — §5.

`test_the_headline_covers_every_direction_and_ranks_none` pins that the master
resume is not framed as one direction and that its order ranks nothing, which is
the property the task asked for.

---

## 10. What is deliberately absent

* **Salary.** `preference_condition-3d50e70c67` records "от $1000 в месяц, прежние
  $1500". It is excluded by kind, and `test_salary_and_search_preferences_stay_out_of_the_public_resume`
  asserts that neither `$1000` nor `1500` appears in the rendered text.
* **Search strategy.** `goal_direction` and `preference_role` are excluded: a public
  resume is not the place for "any suitable IT job quickly" or for how Bitrix24
  ranks against other directions.
* **Personal constraints.** The same exclusion covers them.
* **"15+ years of IT experience"** — §7.

These are excluded from the *public* document only. All five claims stay in the KB
and are printed in the source map's exclusion table, so they remain available to
anything that legitimately needs them — the search filters, the scoring, and later
per-vacancy letters.

---

## 11. Knowledge-base safety, by hashes

| file | SHA-256 (first 16) | note |
| --- | --- | --- |
| `resume.md` | `7ae200cdd7251d3c` | unchanged; T5 never writes it |
| `candidate_profile.json` | `d3b9baf7d2443f7a` | unchanged |
| `ai_assistant/candidate_profile.json` | `652c96170f581fa0` | unchanged |
| `candidate_kb.json` | `fafcedb07c8016e9` | after the project sync, kb_version 27 |
| `ai_assistant/cli.py` | `83806cf0b2a3dee8` | after the byte surgery |

The KB was 2314190140520944… at 101 claims before T5 and is fafcedb07c8016e9 at 122
after. It was restored from `artifacts/candidate_kb.backup_pre_t5.json` and re-synced
once, to clear claims minted before the hyphenation fix in `Anchor.normalized`.

**One honest discrepancy.** `state.db` hashed `52703870871afaad` earlier in T5 and
hashes `2f28334839de4d8c` now, mtime 2026-09-23 10:01:01. This is **not** T5's doing:
`tests/conftest.py` redirects `DB_FILE` to a temp directory for every test, and a
before/after hash across a full run of the T5 test file is byte-identical
(`2f28334839de4d8c` both sides). The write came from a process outside this
repository — the dashboard or a scheduler. T5 code never opens the database.

### The `cli.py` byte surgery

`cli.py` carries 6382 bare CRs in the form `\r\r\n`. The T5 edit was done on bytes,
never through a text editor:

```
sha256   8e3ddfab461ebc40 -> 83806cf0b2a3dee8
bytes    294699 -> 297382  (+2683)
bare CR  6341 -> 6382      (+41)
CRLF     6186 -> 6227      (+41)
reconstruction            EXACT
```

The +41 bare CR and +41 CRLF are the 41 line terminators the inserted block needed,
and `tools/bare_cr_census.py` confirms the file is still the only one that changed:
`cli.py` 6382 bare CRs, `application_queue.py` 769 and `test_submission_verifier.py` 1
— all three unchanged from before T5.

---

## 12. Tests

`tests/test_candidate_master_resume.py` — **59 tests, 59 passed** (48 at T5, 11 added by T5.1).

The ones that matter, by the failure they prevent:

| test | failure it prevents |
| --- | --- |
| `test_master_contains_every_role_from_the_resume` | earlier experience dropped (the G2 finding) |
| `test_master_keeps_every_resume_duty` | a duty lost in restructuring |
| `test_employment_is_reverse_chronological` | roles in source order rather than by date |
| `test_the_former_freelance_heading_is_not_employment` | "Freelance" reading as paid employment |
| `test_no_rendered_claim_is_below_derived_from_source` | a rejected or unconfirmed claim rendered |
| `test_a_rejected_claim_is_excluded_and_reported` | a rejected duty coming back through grouping |
| `test_a_superseded_claim_is_excluded` | a replaced fact surviving beside its replacement |
| `test_a_claim_absent_from_its_source_is_excluded` | a claim whose source no longer has it |
| `test_a_claim_flagged_as_an_assumption_is_excluded` | a guess rendered as a fact |
| `test_salary_and_search_preferences_stay_out_of_the_public_resume` | the salary floor published |
| `test_the_length_is_not_capped` | a truncation losing a fact |
| `test_russian_variant_covers_every_english_line` | a half-translated review document |
| `test_a_missing_translation_is_an_error_not_an_english_fallback` | English silently passing as Russian |
| `test_the_omitted_span_is_still_present_to_be_omitted` | the omission silently stopping |
| `test_independent_role_evidence_is_confirmed_and_bound` | a hand-written claim about the candidate's history |
| `test_chronology_evidence_is_confirmed_and_matches_the_period` | the note drifting from the fact |
| `test_the_headline_covers_every_direction_and_ranks_none` | the master resume framed as one direction, or ordered as if one led |
| `test_every_project_claim_points_at_a_real_quote` | a fabricated project bullet |
| `test_the_catalogue_refuses_to_run_when_an_anchor_moves` | a claim surviving its source moving |
| `test_every_claim_is_rendered_excluded_or_subsumed` | a fact leaving the resume silently (§5) |
| `test_no_claim_is_counted_twice` | the source map crediting one fact to two lines |
| `test_the_build_command_warns_about_an_unaccounted_claim` | an undecided kind passing as clean |
| `test_the_build_command_reports_a_missing_translation_as_an_error` | a traceback instead of a message |
| `test_a_project_sync_ignores_an_incoming_status` | a project claim inheriting a status |
| `test_a_project_sync_marks_a_vanished_claim_absent_without_deleting_it` | the same fact printed twice |
| `test_a_resume_merge_leaves_project_claims_alone` | a re-extract marking project claims absent |
| `test_no_block_line_is_glued_to_a_bullet_above_it` | a block line losing its leading blank and rendering as part of the bullet above it (the `**Stack:**` defect, §1 of the T5.1 review) |

Two of these were **vacuous when first written**, and the fix is worth recording
because the same mistake is easy to repeat:

* `test_a_project_sync_never_confirms_anything` syncs the shipped catalogue and
  asserts every project claim came out `DERIVED_FROM_SOURCE`. The shipped extractor
  already hands over `DERIVED_FROM_SOURCE` records, so the assertion is true by
  construction: mutation `project_sync_forces_derived`, which copies the incoming
  status in, **survived** it. The weak test is kept, and
  `test_a_project_sync_ignores_an_incoming_status` was added beside it — it hands
  the sync a `CONFIRMED_BY_USER` record and asserts the sync declines to believe it.
  The mutation is now caught.
* `test_a_project_sync_is_idempotent` asserted `report.added > 0` against the live
  KB, which already held project claims. It now runs on an empty KB, so it holds
  whatever state the live KB is in.

The general rule: **a test that feeds the code the input the code already expects
proves nothing.** The mutation is what distinguishes the two cases, and it only
does so if the input is adversarial.

A third bug was found by a test rather than by reading: `_children()` grouped a
role's claims without applying the exclusion filter, so a `REJECTED_BY_USER` duty
was rendered. That is now filtered and pinned.

---

## 13. Mutation probes

`tools/t5_guard_mutation.py` — **19 of 19 guards caught their mutation** (11 at T5,
8 added by T5.1), each restoring the original bytes (verified by SHA-256 per
mutation).

| mutation | what it breaks | pin that caught it |
| --- | --- | --- |
| `role_children_filter` | grouping stops filtering excluded claims | `test_a_rejected_claim_is_excluded_and_reported` |
| `project_merge_guard` | a resume merge marks project claims absent | `test_a_resume_merge_leaves_project_claims_alone` |
| `project_sync_forces_derived` | a project claim inherits a status | `test_a_project_sync_ignores_an_incoming_status` |
| `omission_is_silent` | the omitted span returns unnoticed | `test_the_omitted_span_is_still_present_to_be_omitted` |
| `ru_falls_back_to_english` | the Russian document becomes half English | `test_a_missing_translation_is_an_error_not_an_english_fallback` |
| `catalogue_drift_ignored` | a drifted anchor is accepted | `test_the_catalogue_refuses_to_run_when_an_anchor_moves` |
| `subsumed_bucket_dropped` | four claims leave the account silently | `test_every_claim_is_rendered_excluded_or_subsumed` |
| `meta_reuses_heading_claims` | the map credits one fact to two lines | `test_a_role_heading_and_its_meta_line_share_no_claim` |
| `freelance_employer_restored` | "Freelance" claims paid work | `test_the_former_employer_name_is_excluded_with_a_reason` |
| `independent_facts_dropped` | the period loses its own facts | `test_the_independent_role_renders_its_own_facts` |
| `unaccounted_gate_disabled` | an undecided claim passes as clean | `test_the_build_command_warns_about_an_unaccounted_claim` |

The table above lists the eleven T5-era probes. The eight added by T5.1 —
`period_attached_to_whole_block`, `chronology_employer_names_a_stranger`,
`headline_gets_a_lead`, `summary_identity_restored`,
`proposal_published_as_the_summary`, `proposals_section_dropped`,
`status_anchor_claim_overstated` and `meta_line_glued_to_the_bullet_above` — are
listed with their pins in §8 of the T5.1 review, so the two tables together account
for all nineteen.

The tool's own verdict logic was fixed in this task. It previously read
`"error" in output.lower()`, which a run that died during collection would satisfy,
and reported `SURVIVED` for a run that never happened. It now requires a pytest
summary line and distinguishes three states: `CAUGHT`, `SURVIVED`, and `INVALID`
(anchor stale, no summary, or restore failed). A measurement that did not happen is
not a measurement, and calling it `SURVIVED` would have hidden exactly the failure
the tool exists to catch. The first run of the extended tool did produce one
`INVALID` — the `independent_facts_dropped` anchor matched two places — and it was
correctly reported as proving nothing rather than as a survivor.

---

## 14. Regression

Two full runs. The first was launched before the last two tests existed; the second
is against the final code.

```
run 1   1 failed, 2267 passed in 1390.53s (0:23:10)   EXIT=1
run 2   1 failed, 2281 passed in 1595.33s (0:26:35)   EXIT=1
```

junit for run 2: **2282 tests, 1 failure, 0 errors, 0 skipped**, of which **48** are
`test_candidate_master_resume` and all 48 passed.

**The single failure is pre-existing and unrelated to T5:**

```
FAILED tests/test_report_line_refs.py::test_the_report_has_no_new_symbol_mismatches
flagged for reading: 24
assert 24 <= 18
```

That is the advisory ratchet on `docs/ble001_triage.md`, red since before T5 at
**24 flagged against a threshold of 18**. It is a property of the BLE001 triage
report, not of the candidate work.

T5's own contribution to it was measured rather than argued: with the pre-T5
`cli.py` swapped back in, the full output, the `--symbols` output and the exit code
are byte-identical. The 24 flagged rows are the same rows, and neither flagged
`cli.py` reference (lines 3309 and 448) lies past the T5 insertion point. **Delta 0.**

**T5.1 re-ran the suite on the post-fix code: 2293 tests, 1 failure, 0 errors** — the
same ratchet test with the same number, and all 59 `test_candidate_master_resume`
cases passing. The verdict came from the junit file rather than the console: the run
reached `[100%]` and was then killed by the deletion watcher during cleanup, so the
log carries no summary line and `EXIT=1` is not a verdict. Full detail in §9 of the
T5.1 review. The same watcher killed the first attempt to run all nineteen mutations
in one shell call — all nineteen came back `INVALID` with no summary, which the tool
correctly refused to read as `SURVIVED`; re-run in four batches across separate shell
calls they were 19/19 `CAUGHT`.

The `EXIT=1` is the pre-existing failure, not the safe-delete guard: both runs
printed a full summary. Per the project's convention the exit code is not the
verdict — the junit file is, and both were written during the session.

Run 2 also settled the `state.db` question from §11 for the whole suite, not just
for the T5 file:

```
state.db BEFORE  2f28334839de4d8c
state.db AFTER   2f28334839de4d8c
```

2282 tests ran and the database is byte-identical on both sides.

---

## 15. Limitations

* **The Russian variant is a translation, not a second source.** It is generated
  from the same lines and the same claims; if a translation is wrong, the English
  line is right and the Russian one misleads a human checker. `RU_TRANSLATIONS` is
  hand-written, so it can be wrong in a way no test detects. This is why the task
  asked for it as a *review* document rather than as a deliverable.
* **The headline is a proposal.** Three directions, ordered by nothing but the
  author's judgement. It needs the candidate.
* **Subsumption is a judgement.** Saying the `title` claims are "represented by the
  headline" is defensible, not provable. The reasons are printed so a human can
  disagree.
* **The project statuses are the repositories' own words.** "IMPLEMENTED" is a
  README's claim, anchored to a line, not an independent assessment. Repiton is
  labelled `PROTOTYPE` because its own catalogue entry says so.
* **`candidate-kb verify` and `candidate-audit` were diffed, not re-derived.**
  Byte-identical output is strong evidence that T5 did not change their behaviour;
  it is not a proof about every code path.
* **The summary still opens with "AI Automation Engineer".** That is the candidate's
  own wording from `resume.md`, and T5 may not reword a claim. It sits awkwardly
  under a three-direction headline, and it is the first thing a recruiter reads.
  §17 asks about it.

---

## 16. What T5 did not touch

Not written, not opened, not changed: `resume.md`, `candidate_profile.json`,
`state.db`, the live filters, the application pipeline. No real application was
sent, no paid API was called, nothing was pushed. `git push` was not run and T6–T8
were not started.

The only files T5 wrote are the four under `artifacts/` and the four new/modified
sources listed in §3.

---

## 17. Remaining genuinely important clarifications

**T5.1 reduced this list from eight to three.** Items 1, 2, 4, 6 and 8 below were
either fixed in the generator or turned into declared proposals; items 3, 5 and 7
were re-scoped. The current list — three decisions, each of which changes the
published document — is in
`docs/t5_1_master_resume_content_review.md` §10:

1. the professional summary's opening (new prose, needs approval);
2. whether to state a tenure figure, and which;
3. `in production` versus a softer phrase.

What the original eight became:

| original | outcome |
| --- | --- |
| 1. summary's opening | **fixed** — the single-direction opening is a declared omission, and a neutral replacement is proposed |
| 2. headline order | **fixed** — four directions, alphabetical, so the order ranks nothing |
| 3. total IT tenure | **open** — carried forward as clarification 2; the three numbers are explained and `15+` is proposed with its arithmetic |
| 4. Speedcast/Germsat block | **fixed** — the period is attributed to that company alone and the block keeps its own dates, so the timeline is continuous and no gap is shown |
| 5. dates for the other three employers | **no action needed** — absent from every source, and the draft states no date for them. Nothing about the years between has to be explained |
| 6. "in production" | **open** — carried forward as clarification 3 |
| 7. publishing the contacts | **unchanged** — both come from `resume.md` and are rendered; confirm they are the ones to publish |
| 8. WHUBN and Repiton ordering | **fixed** — the status labels are rendered and now noted as the repositories' own words, not an independent assessment |

---

## 18. Proposals for separate IT-targeted versions

The master resume is the source, not the output. Five versions are supportable from
the knowledge base as it stands, with no new facts:

| version | leads with | demoted | still missing |
| --- | --- | --- | --- |
| **IT Support / Application Support** | the 2009–2021 infrastructure block: Windows Server, Active Directory, Exchange, SQL, Hyper-V, networking, IP telephony, monitoring, ITSM; Bash/PowerShell automation; Avantis support work | LLM and n8n detail | dates/titles for three of the four early employers |
| **Bitrix24 / CRM implementation & support** | Avantis (self-hosted administration, integrations, business processes, robots) + Sayama (workflows, telephony, email, analytics, lead-generation channels, voice pipeline into deal records) | Python internals | whether the work was implementation, support or both; any user counts |
| **Python / Automation Engineer** | Sayama + the own-projects period: asyncio, aiogram, Telethon/MTProto, REST/webhooks, Docker/Linux, n8n/Make, testing | the 2009–2018 block, condensed | a code sample or a public repository |
| **AI / LLM Engineer** | the current draft, essentially: agents, RAG, prompt engineering, Gemini/OpenAI/Anthropic, fact-checking | the infrastructure block | whether "in production" may be said (clarification 6) |
| **Backend (Python), junior/middle** | SQL, PostgreSQL, MS SQL, asyncpg/SQLAlchemy, asyncio, REST | Bitrix24 specifics | a backend project that is not an automation pipeline |

Two caveats on all five:

* **English B1** is rendered and should stay rendered. It is the constraint most
  likely to end a conversation early, and hiding it wastes both sides' time.
* **No formal computer-science education.** The degree is "Technology and
  Entrepreneurship" (2005). It should stay as stated; the IT-support version is the
  one where this matters least.

The generator already has what these need: `build_master_resume()` returns a
structured `MasterResume` whose lines carry claim ids and kinds, so a per-vacancy
version is a selection and an ordering over the same lines rather than a second
document. That is T6/T7 work.

---

## 19. Readiness for T6

T5 is complete and measured. The knowledge base is at 122 claims with 0 unaccounted
and 0 double-credited; the draft is 105 lines and renders every role, duty and
skill the resume stated; the Russian variant covers every English line; the source
map lets a human check any line against a file, a line number and a quote.

Two facts remain `PENDING` in the interview session
(`goal_direction-d40c706712`, `constraint_condition-c9dab9fb4e`). They are search
strategy and are excluded from the public resume by kind, so they do not block T5 —
but they do block T6's job-to-candidate matching, which is what needs them.

**Stop here for the candidate's approval**, as the task requires. The four
declarations in §9 and the eight clarifications in §17 are the ones worth reading
before T6 starts.
