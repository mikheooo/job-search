# T4.5 — Candidate Interview

Status: **implemented and measured**. New stage, sitting between T4 (audit) and T5
(master resume). It is the first stage in the chain that *asks the candidate
something*; T1–T4 only read documents.

Line references in this file are in the `grep -n` coordinate system. `interview.py`,
`interview_questions.py`, `interview_extract.py` and `tests/test_candidate_interview.py`
are LF-only (CRLF count 0), so the coordinate systems agree for them. `cli.py` is
not: it carries a bare CR in its header and `\r\r\n` in its body, and every edit to
it in this task was byte surgery (see §12).

---

## 1. Why this stage exists

T4 left the candidate with 27 findings and T3 with 10 contradictions, and neither
could resolve a single one of them, by design: *verification detects, never
resolves.* Ten of T3's contradictions are really three questions, and the audit
found a 27-month gap and a missing salary figure that no rule may invent an answer
to.

So T4.5 is a **step-by-step plain-language interview** that:

* builds its questions from the audit that was actually run (T3's contradictions,
  T4's findings, and the knowledge base itself) — never from a hardcoded list;
* asks **one question per step**, with `clarify`, `skip`, `back` and `progress`;
* persists progress across sessions;
* turns each answer into **atomic facts with evidence**, and shows them for
  confirm / edit / reject;
* treats **receiving an answer as not a confirmation**;
* distinguishes **experience** from **career preference**, structurally;
* never invents a number, a duration, an employer or a technology.

It is placed before T5 because the master resume T5 builds cannot be generated
from documents that contradict each other and from gaps nobody has filled.

---

## 2. What was reused rather than rebuilt

The brief is explicit that this stage must not duplicate the knowledge base, the
confirm/reject transitions, the versioning or the audit. Concretely, T4.5 reuses:

| Reused | Where from | How |
|---|---|---|
| the knowledge base | T2 `kb.py` | there is **one** KB; interview facts are claims in it |
| `confirm` / `reject` | T2 `kb.py` | called unchanged; still the only status transitions, still idempotent |
| `ClaimStatus` | T2 `kb.py` | `DERIVED_FROM_SOURCE` / `CONFIRMED_BY_USER` / `REJECTED_BY_USER`, no new states |
| content-derived claim ids | T1 `resume_parser._IdFactory` | reimplemented as `make_claim_id`, with a test that recomputes a parsed claim's id through it |
| `ClaimRecord` fields | T2 `kb.py` | `role_id` / `parent_id` carry the role/project linkage; `evidence` carries the quote |
| T3's result object | T3 `kb_verify.VerificationResult` | passed through via `run_audit`, not re-derived |
| T4's findings | T4 `audit.run_audit` | the plan is grounded in exactly the audit the candidate can see |
| the profile loader | T3 `kb_verify` | `load_profile` / `load_baseline_profile` / `resolve_profile_path` |
| the LLM entry point | `job_analyzer._call_llm` | provider config, retry policy and `JOB_ANALYZER_OFFLINE` short-circuit, imported lazily |

**No second knowledge base exists.** There is one `candidate_kb.json`, and an
interview fact is a `ClaimRecord` in its `claims` dict with
`evidence.source_file == "interview"`.

---

## 3. Files

| File | Bytes | sha256[:16] | Lines | New? |
|---|---|---|---|---|
| `ai_assistant/candidate/interview.py` | 45890 | `34969e8430c72895` | 1187 | new |
| `ai_assistant/candidate/interview_questions.py` | 35786 | `5b18e1f78d278757` | 942 | new |
| `ai_assistant/candidate/interview_extract.py` | 21114 | `fc87935e2b95a1b2` | 577 | new |
| `ai_assistant/candidate/kb.py` | 16189 | `9dfae4927dd0d421` | 426 | **modified** |
| `ai_assistant/cli.py` | 294699 | `8e3ddfab461ebc40` | 6186 | **modified** |
| `tests/test_candidate_interview.py` | 60613 | `fc9af234376bb002` | 1614 | new |
| `tools/kb_invariant_mutation.py` | 27914 | `bde047e26dc2dc2e` | 646 | **modified** |
| `tools/cr_payloads/cli_candidate_interview_argparse.txt` | 2511 | `430ea482c62a5301` | 57 | new |
| `tools/cr_payloads/cli_candidate_interview_dispatch.txt` | 788 | `1b7f4b4dbfae3703` | 16 | new |
| `docs/t4_5_candidate_interview_report.md` | this file | — | — | new |

### 3.1 Three modules instead of one

The brief does not name a file list. The split follows three genuinely separate
contracts, each of which a test can exercise on its own:

* **`interview_questions.py`** — what to ask and why. Pure: no I/O, no model, no
  clock. Its contract is "every question cites its ground", enforced in
  `Question.__post_init__`.
* **`interview_extract.py`** — turning an answer into validated atomic facts. Its
  contract is "no fact may exist that the candidate did not write".
* **`interview.py`** — session state, decisions and persistence. Its contract is
  "nothing moves a status except an explicit per-fact decision".

Collapsing these would have put the anti-fabrication rules and the state machine in
one file, which is exactly where a later "small" change could weaken a net without
anyone noticing.

---

## 4. The T2 knowledge-base model, checked before integrating

The brief requires checking the KB model first, and the check changed the design.
Reading `kb.py` in full, four things in `merge_claims` (T2, `kb.py:268`) matter:

1. it **sets** `self.source_file`, `self.source_sha256` and `self.updated_at`;
2. it **bumps** `kb_version`;
3. it marks **every claim not in the incoming list** as `in_source = False`;
4. it never touches a status.

Consequence 3 is the trap the brief names. Routing interview facts through
`merge_claims` would mean handing it a list of *interview* claims, whereupon all 96
resume claims are "not in the incoming list" and get marked absent — and
consequence 1 would relabel the resume snapshot as an interview. **So the
interview never calls `merge_claims`.**

### 4.1 The schema decision: no envelope change, one guard

`kb_format_version` stays at **1**. Nothing about the JSON shape changes: an
interview fact is an ordinary `ClaimRecord` whose `evidence` dict carries
`source_file: "interview"` plus `question_id`, `session_id`, `answered_at` and
`extractor`. The `evidence` field was already a free-form dict, so no migration of
the old JSON is needed — and `tests/test_candidate_kb.py:385` still pins
`kb_format_version == 1` and still passes.

The one change to `kb.py` is a **guard** in both loops of `merge_claims`, so a
resume re-extract cannot eat an answer:

* `kb.py` — the incoming loop: a claim that `is_interview_sourced` is not refreshed
  by the resume merge, even when the same words appear in the resume. Refreshing
  would replace its evidence with resume provenance, and the confirmation it
  carries would then look like it came from the parser.
* `kb.py` — the absent loop: an interview fact is **not** "absent from the source".
  The resume never had it to lose, and marking it absent would make every
  downstream filter that reads `in_source` quietly drop a confirmed answer.

`MergeReport` gained `skipped_foreign: int = 0`, printed in `summary()` **only when
non-zero**, so a resume-only merge prints byte-identical text to before
(`tests/test_candidate_kb.py:517` pins a substring of it and still passes).

Both guards are unreachable for any pre-T4.5 knowledge base, because no claim
carried `evidence.source_file == "interview"` before this stage. That is why all
96-claim behaviour is preserved exactly, and the T2 suite is green unchanged.

### 4.2 The session file

The brief distinguishes "the temporary KB" from "session files", and it is right
to: the *session* is a different kind of thing from a *fact*.

`artifacts/candidate_interview_state.json` holds the plan, the cursor, every answer
and every undecided fact. It carries its own `session_format_version` (1) and is
the only place an **unconfirmed** fact ever exists. `artifacts/` is git-ignored and
already holds this project's other state files (`hh_message_reply_state.json`).

Migration is explicit and one-directional: a file with **no** version field is read
as version 1 (that is what version 1 looks like), unknown keys are ignored, and a
**future** version is refused with a `SessionFormatError` rather than guessed at —
guessing is how a confirmation gets silently dropped.

---

## 5. The dialogue scenario

**To start the interview, run exactly this.** The repository's single entry point is
`python -m ai_assistant.cli` (per the README); nothing else needs starting, no server,
and no environment variable — the LLM extractor defaults to `off`:

```
$ .venv/Scripts/python.exe -m ai_assistant.cli candidate-interview start
interview interview-20260922T114058Z-3f9a1c: 19 question(s) in 5 section(s).
```

Every other command below is that same invocation with a different action; the shorthand
`candidate-interview <action>` stands for `.venv/Scripts/python.exe -m ai_assistant.cli
candidate-interview <action>`. Progress is saved after every turn, so the session can be
abandoned and resumed with `candidate-interview start` (it picks up where it stopped;
`--reset` begins a new one).

```
[1/19] Professional goals and constraints

In your own words: what kind of work do you want to be doing a year from now?

$ candidate-interview clarify
[1/19] Professional goals and constraints
...
why this is being asked: Your profile lists target roles (AI Automation Engineer,
AI Automation Developer, n8n Developer, Automation Engineer...), but that list was
written by an earlier step rather than by you. Your answer here becomes the yardstick
every vacancy is measured against.
an answer here is recorded as: goal_direction (GOAL)
grounded in:
  - profile:target_roles -- AI Automation Engineer, AI Automation Developer, ...

$ candidate-interview answer "I want to build AI automation systems: n8n pipelines, RAG, Telegram bots.
Mostly for content processing and lead generation."
extracted facts (PENDING): 2
  goal_direction-04008c5952  [PENDING] goal_direction
    fact  : I want to build AI automation systems: n8n pipelines, RAG, Telegram bots.
    quote : I want to build AI automation systems: n8n pipelines, RAG, Telegram bots.
  goal_direction-b36e5702d0  [PENDING] goal_direction
    fact  : Mostly for content processing and lead generation.
    quote : Mostly for content processing and lead generation.

Nothing has been written to the knowledge base. Confirm each fact you accept with
`candidate-interview confirm --fact <id>`.

$ candidate-interview confirm --fact goal_direction-04008c5952
confirm: goal_direction-04008c5952
  kind   : goal_direction (GOAL)
  value  : I want to build AI automation systems: n8n pipelines, RAG, Telegram bots.
  quote  : I want to build AI automation systems: n8n pipelines, RAG, Telegram bots.
  status : CONFIRMED
  written to knowledge base: .../candidate_kb.json

$ candidate-interview confirm --fact goal_direction-04008c5952
confirm: goal_direction-04008c5952 is already CONFIRMED (no change, nothing written)

$ candidate-interview edit "I want to build AI automation systems: n8n pipelines and RAG." --fact goal_direction-04008c5952
edited: goal_direction-04008c5952 -> goal_direction-71b9a11287
  new value : I want to build AI automation systems: n8n pipelines and RAG.
  status    : PENDING

The corrected fact is PENDING and needs its own confirmation: an edited fact is a
different fact, so there was no confirmation for it to inherit.

$ candidate-interview skip
skipped: no fact was recorded, and nothing was written to the knowledge base.

[2/19] Professional goals and constraints

What must be true about a job for you to take it, and what must never be true?

$ candidate-interview progress
interview progress
  session        : interview-20260922T114058Z-3f9a1c  (started 2026-09-22T11:40:58+00:00)
  state file     : .../interview_state.json
  knowledge base : .../candidate_kb.json
  questions      : 1/19 answered, 1 skipped
  current        : q.goals.constraints
  facts          : PENDING 1, CONFIRMED 1, REJECTED 0, SUPERSEDED 1

  sections:
    Professional goals and constraints     1/4
    Career chronology                      0/5
    Projects and achievements              0/2
    Technologies and depth                 0/2
    Open questions from the audit (T3/T4)  0/6
```

Actions: `start` `answer` `confirm` `reject` `edit` `next` `back` `skip` `clarify`
`progress` `facts`. All accept `--kb`, `--session`, `--profile`, `--resume`,
`--json`; `start` also takes `--reset` and `--rebuild`, and `--llm` selects the
extraction mode.

### 5.1 Two argument-order facts worth writing down

* With an optional positional (`value`) plus options, **argparse consumes the
  positional pattern before the flags**. `candidate-interview edit "new text"
  --fact <id>` works; `candidate-interview edit --fact <id> "new text"` does not.
  The dispatch therefore merges `--text` and the positional, and the CLI reports
  a clear error rather than a traceback when neither is given.
* An answer that **starts with a dash** (a bullet list) cannot go through the
  positional at all — argparse reads `- remote only` as an unknown option, and
  `--` does not help because the positional pattern was already consumed. Use
  `--text`:

```
candidate-interview answer --text "- remote only
- no night shifts" --kb ... --session ...
```

That path is pinned by `test_the_cli_accepts_dash_prefixed_text_through_the_text_option`,
which runs the real `cli.main()`.

---

## 6. The plan: 19 questions, all grounded

Built from the live sources: 96 in-source claims, 27 T4 findings, 10 T3
contradictions.

| Section | Questions |
|---|---|
| Professional goals and constraints | 4 |
| Career chronology | 5 |
| Projects and achievements | 2 |
| Technologies and depth | 2 |
| Open questions from the audit (T3/T4) | 6 |
| **Total** | **19** |

Grounding entries by source: **t3 ×11, t4 ×17, kb ×6, profile ×2**. Fact classes:
EXPERIENCE 12, PREFERENCE 3, GOAL 3, CONSTRAINT 1.

All four T3 rules that fired are asked about:
`total_experience_vs_it_support`, `bitrix_experience_vs_excluded`,
`years_experience_vs_domain_years`, `baseline_vs_profile_drift`.

Six of the seven T4 rules that fired are asked about. The seventh,
`tech_contradicted_claim`, is deliberately **not** given a question of its own: it
is T4 restating T3's contradictions (the tech reviewer's mandatory obligation), so
asking again would be asking the same question twice under two ids.

---

## 7. The three T3 ambiguities, and the experience/preference split

The brief names three things no rule may settle, because settling them means
choosing a meaning for the candidate's own words. Each gets a question:

| Question | Conflict | Asks |
|---|---|---|
| `q.conflict.tenure_total` | resume `15+ years of IT experience` vs profile `domain_years.it_support = 11` | how many years in IT, and what the number includes |
| `q.conflict.tenure_meaning` | profile `years_experience = 3` vs five larger domain figures (11, 9, 5, 3.5, 3.5) | what the `3` is meant to represent |
| `q.conflict.bitrix_experience` | resume leads with Bitrix24 work | what the candidate actually did with Bitrix24 |
| `q.conflict.bitrix_preference` | profile lists `bitrix` under `excluded_roles` and `must_avoid_conditions` | whether to keep presenting it, and whether to keep avoiding it |
| `q.conflict.target_roles` | profile file vs the code's built-in fallback (3 fields) | which list is current |
| `q.conflict.headline.1` | `AI Engineer \| AI Automation Engineer \| Python Developer` | which title an ATS should see first |

### 7.1 Why Bitrix24 becomes two questions

One contradiction, two questions, two **different fact classes**:
`experience_note` (**EXPERIENCE**) and `preference_role` (**PREFERENCE**). This is
the structural half of "distinguish experience from career preference": a later
stage cannot paste a preference into the experience section of a resume, because
the class is on the record and `is_experience_kind()` answers for it. The two
questions sit next to each other in the plan precisely so the candidate can see
that they are different questions.

### 7.2 Absence is not an error

The brief requires that a missing salary and a date gap not be treated as errors.
Both questions say so in their own text, and a test pins the wording:

* the gap question's `why` opens **"A gap is a question, not a mistake."** and notes
  that *"I was not working" is also an answer*;
* the salary question's `why` says **"Saying no is a complete answer"** — declining
  to state a figure is recorded as a decision about the candidate's own document,
  not as a defect;
* skipping either question writes nothing at all to the knowledge base.

---

## 8. An answer is not a confirmation

This is the brief's central rule, and it is enforced by *where things are written*,
not by a check.

```
answer      -> session file only           (KB byte-identical: pinned by hash)
extract     -> proposals, status PENDING   (nothing in the KB)
confirm(id) -> that ONE claim -> CONFIRMED_BY_USER
reject(id)  -> that ONE claim -> REJECTED_BY_USER   (record kept)
edit(id)    -> new id, PENDING again; the old proposal becomes SUPERSEDED
skip        -> session file only           (KB byte-identical: pinned by hash)
```

The knowledge base receives an interview fact **only** at the moment the candidate
explicitly confirms or rejects it. `test_answering_writes_nothing_to_the_knowledge_base`
hashes the KB around `answer`; `test_answering_does_not_confirm` asserts every
proposal is still `PENDING` and that the KB holds zero interview claims.

### 8.1 Editing, and why a revision cannot inherit a confirmation

`claim_id` is content-derived (`sha256(kind ␟ scope ␟ value)[:10]`). Correcting a
fact therefore produces a **new id** — a genuinely different fact — so there is no
confirmation for it to inherit. That is the mechanism behind "a corrected fact
requires re-confirmation"; it is not a policy bolted on top.

The subtle case is revising an *already confirmed* fact. Three rules keep it
honest:

1. the original claim **keeps `CONFIRMED_BY_USER`** — nothing is demoted, because
   the record of the confirmation is still true;
2. the original gains `evidence.superseded_by` **only when the revision is itself
   confirmed** — so rejecting the revision leaves the original effective;
3. the old proposal becomes `SUPERSEDED`, and a superseded fact is **not
   decidable** (`confirm`/`reject`/`edit` refuse it and name the replacement).
   Without that guard, rejecting a superseded fact would flip a confirmed claim to
   rejected for no reason a reader could reconstruct.

---

## 9. Anti-fabrication: three nets, and what they do not catch

`validate_fact` refuses a candidate fact unless all of these hold:

1. **verbatim** — `quote` must be a literal substring of the answer the candidate
   typed. Same guarantee as T1's resume evidence, moved to the interview.
2. **numbers** — every number in `value` must appear in the quote, or in the
   question itself (a question may legitimately supply a figure, e.g. "your profile
   records 11 years"). This is the net against inventing durations and totals.
3. **technology** — any known technology named in `value` must also appear in the
   quote, using the resume's own skill vocabulary as the term set. This is the net
   that stops *"I automated some reports"* becoming *"knows Python"*.

Plus: the kind must be one of the nine allowed kinds, and the fact must not be
empty. A refusal returns a **reason**, and the reason is shown to the user — a
rejection that cannot say why is indistinguishable from a bug.

### 9.1 What the nets do not catch

An extractor could still produce a *paraphrase* that adds no number and no known
technology yet is still wrong. The nets keep the thing being shown honest; they do
not replace the candidate. The real defence is that extraction is not confirmation:
every fact is displayed with its quote, and only an explicit per-fact confirmation
moves it. This limitation is stated here rather than glossed over.

### 9.2 The deterministic mode invents nothing by construction

In the default mode `value` **is** `quote`: the answer is split into the statements
the candidate wrote (list markers stripped) and each becomes one fact, verbatim. A
repeated sentence collapses to one fact. There is no step at which a number, a
technology or an employer could enter a fact that the candidate did not type — so
*"I don't know."* becomes exactly one fact reading *"I don't know."*, never an
achievement.

---

## 10. The model: available, validated, and off by default

The project's existing entry point is reused — `job_analyzer._call_llm`, imported
lazily, which already owns the provider configuration, the retry policy for
transient 5xx/429, and the `JOB_ANALYZER_OFFLINE` short-circuit. There is no new
HTTP code and no new API key handling.

| Mode | `CANDIDATE_INTERVIEW_LLM` | Behaviour |
|---|---|---|
| **`off` (default)** | unset / `off` / anything unrecognised | deterministic extractor; the model is never called |
| `auto` | `auto` | try the model; on failure fall back to deterministic **and say so** |
| `required` | `required` | try the model; on failure raise `ExtractionError` with the cause |

`JOB_ANALYZER_OFFLINE=1` forces `off`. **The default is deliberately the dull one**:
this project has no budget for accidental spend, and a test suite that silently
reached a paid endpoint would be worse than a duller extractor. A garbage value in
the environment falls back to `off` rather than erroring — the failure mode of a
typo should be "a duller extractor", never "a surprise invoice".

The model path is still fully exercised, by **injecting** a fake caller:
`extract_facts(..., llm=...)`. That covers prompt building, JSON parsing, the
per-fact validation and the refusals, with no socket and no bill. An injected
caller wins over the mode, which is what makes those tests possible.

`parse_llm_payload` refuses anything that is not the promised shape (not JSON, not
an object, no `facts` key, `facts` not a list, a fact not an object) with a
specific message each time.

---

## 11. Knowledge-base safety, by hashes

Measured, not asserted. Around a full interview run (answer + confirm + reject +
edit + skip) against a temporary KB:

| Check | Result |
|---|---|
| all 96 resume claims (`id`, `status`, `in_source`, `evidence`) | **byte-identical** |
| `source_file` | unchanged (`resume.md`) |
| `source_sha256` | unchanged (`7ae200cdd7251d3c…`) |
| `updated_at` | unchanged |
| interview claims written | only those confirmed/rejected, with `source_file: "interview"` |

And the guard, measured directly: replaying `candidate-kb extract`'s merge against
a KB that holds a confirmed interview fact gives

```
merge report: kb_version 2 -> 3, added 0, refreshed 96, marked absent 0,
              still absent 0, left alone 1 interview fact(s)
```

The interview claim survives with its status, `in_source = True` and its `interview`
provenance; `refreshed 96` / `marked absent 0` shows no T2 behaviour moved.

The five protected files (`resume.md`, `candidate_profile.json`,
`ai_assistant/candidate_profile.json`, `candidate_kb.json`, `state.db`) are hashed
around a full run by `test_a_full_interview_leaves_every_protected_file_untouched`,
and `test_the_repo_knowledge_base_is_not_written` pins the production KB
specifically. **The production KB was not mutated by this task**: it was
`2018a7a464fa959a` before the first command and `2018a7a464fa959a` after the last.

---

## 12. The `cli.py` byte surgery

`cli.py` is not a normal text file: bare CR separates its header, `\r\r\n` its body.
Every edit went through `tools/cr_safe_insert.py`, which takes the separator from
the neighbouring line rather than imposing one and then proves the edit.

| Step | sha256 before → after | Bytes | Lines | bare CR / CRLF delta |
|---|---|---|---|---|
| insert argparse block before line 4992 | `011338ca96bffe74` → `0a655e8666073009` | +2413 | +50 | +50 / +50 |
| insert dispatch block before line 5464 | `0a655e8666073009` → `65f3e5fb608a81d9` | +789 | +16 | +16 / +16 |
| replace argparse block 4992–5041 (add `--text`) | `65f3e5fb608a81d9` → `2aec151bdd52256c` | +212 | +7 | +7 / +7 |
| replace dispatch block 5471–5486 (merge `--text`) | `2aec151bdd52256c` → `8e3ddfab461ebc40` | +31 | +0 | 0 / 0 |

Net: **+73 lines**, 6113 → 6186. The `--replace-range` steps confirmed the block
boundaries by printing the old first and last lines, which matched the payload's
exactly. `ast.parse` succeeds on the result, and the wiring is exercised by three
tests that run the real `cli.main()` with `sys.argv` monkeypatched.

---

## 13. Tests

`tests/test_candidate_interview.py` — **117 tests**, all passing. Against the
brief's list:

| Required pin | Test(s) |
|---|---|
| questions grounded in T3/T4 | `test_every_question_carries_grounding`, `test_the_three_t3_ambiguities_each_get_a_question`, `test_every_firing_t3_rule_gets_a_question`, `test_questions_are_grounded_in_t4_findings`, `test_the_bitrix_conflict_splits_into_experience_and_preference` |
| one question per step | `test_one_question_per_step`, `test_the_step_counter_matches_the_plan_length` |
| explicit individual confirmation | `test_confirm_moves_exactly_one_fact`, `test_confirming_one_fact_does_not_touch_another`, `test_confirm_is_idempotent` |
| edit then re-confirm | `test_edit_creates_a_new_pending_fact`, `test_an_edited_fact_needs_its_own_confirmation`, `test_revising_a_confirmed_fact_keeps_the_original_until_the_revision_is_confirmed`, `test_rejecting_a_revision_leaves_the_original_effective`, `test_a_superseded_fact_cannot_be_decided` |
| answer idempotency | `test_answering_twice_with_the_same_text_is_idempotent`, `test_re_answering_does_not_demote_a_confirmed_fact` |
| skip and continue | `test_skip_advances_and_writes_nothing`, `test_skip_does_not_produce_facts`, `test_skipping_the_whole_plan_completes_it`, `test_back_keeps_every_decision` |
| resume KB facts preserved | `test_a_full_interview_leaves_every_resume_claim_untouched`, `test_the_envelope_keeps_the_resume_snapshot`, `test_a_resume_merge_leaves_interview_facts_alone`, `test_a_resume_merge_still_refreshes_every_resume_claim`, `test_the_merge_summary_is_unchanged_when_there_is_no_interview_data` |
| no auto-promotion | `test_answering_does_not_confirm`, `test_answering_writes_nothing_to_the_knowledge_base` |
| no invented facts / achievements | the five `validate_fact` nets, `test_deterministic_facts_are_verbatim`, `test_a_refusal_is_recorded_as_a_refusal_not_an_achievement`, plus three injected-model refusals |
| temp KB and session isolation | `test_a_full_interview_leaves_every_protected_file_untouched`, `test_the_default_session_file_is_not_created_by_a_test`, `test_the_engine_writes_only_the_paths_it_was_given`, `test_the_repo_knowledge_base_is_not_written` |
| no change to the application pipeline | the protected-file hashes above; nothing in `application_prep.py`, `job_analyzer.py`, `state.db` or the sending path is imported or written |

Plus session persistence and migration (round-trip, atomic save with no `.tmp`
leftover, no-version file read as v1, future version refused, unknown keys ignored,
broken file reported), the LLM modes, and the CLI layer.

### 13.1 A false pass I caught, and how

The first run was `107 passed, 9 failed`. Eight of the nine failures shared one
cause: `EXTRACT_SYSTEM_PROMPT.format(kinds=...)` raised `KeyError: '"facts"'`,
because the prompt is mostly literal JSON and `str.format` read its braces as
placeholders. Fixed by substituting `__KINDS__` with `str.replace`.

The interesting part is that **two tests had been passing for the wrong reason**:
`test_auto_mode_falls_back_to_deterministic_with_a_note` and
`test_required_mode_raises_when_the_model_is_unavailable` both accepted *any*
exception as "the model was unavailable", so a crash inside prompt building looked
exactly like an unreachable model. Both now assert the **cause** appears in the
message (`RuntimeError`), so that class of false pass cannot recur.

---

## 14. Mutation probes

`tools/kb_invariant_mutation.py` grew from 13 to **22 probes**; `CALIBRATION` was
re-derived for the modified `kb.py` and extended with the three new modules. All 22
have a unique anchor and a calibrated source (the tool's own dry run is the gate).

Nine new probes, each introducing the bug the pins exist to catch. **All nine
CAUGHT**, each with a byte-identical restore:

| Probe | Bug introduced | Pins that went red |
|---|---|---|
| `interview_answer_confirms` | an answer confirms everything it extracted | 2 |
| `interview_merge_guard` | a resume re-extract marks interview facts absent | 1 |
| `interview_edit_inherits` | an edit inherits the confirmation it replaced | 1 (of 2) |
| `interview_net_verbatim` | a fact may quote text never written | 2 |
| `interview_net_numbers` | a fact may state a number never given | 2 |
| `interview_net_technology` | a fact may name a technology never mentioned | 2 |
| `interview_llm_default` | a paid model becomes the default | 2 |
| `interview_grounding_required` | a question may cite no ground | 1 |
| `interview_t3_question` | a T3 ambiguity is silently dropped | 2 |

Two honest observations:

* `test_edit_creates_a_new_pending_fact` **passed** under
  `interview_edit_inherits`. That is correct, not a hole: it edits an
  *unconfirmed* fact, so it cannot discriminate a mutation about inheriting a
  confirmation. The `any()` criterion caught the mutation via the other pin. The
  probe keeps the insensitive pin listed so its insensitivity stays visible.
* The T2 probes against the modified `kb.py` were **re-run** after the change
  (`promote`, `forget`, `ids`, `audit_readonly` — all CAUGHT, `candidate_kb.json`
  restored to `2018a7a464fa959a`). The remaining T3/T4 probes were not re-executed
  this session because their targets are byte-unchanged since T4; the dry run
  validated their anchors and calibration.

After every probe, the twelve touched files were re-hashed and all twelve matched
their expected digests: nothing was left mutated.

---

## 15. Regression

Full suite, run after every source change was frozen.

| Metric | T4 baseline | T4.5 |
|---|---|---|
| tests | 2117 | **2234** (+117) |
| failures | 1 | **1** (the same test) |
| errors | 0 | **0** |
| duration | 1460.5 s | 1491.9 s (24.9 min) |
| junit | `.pytest_t4_full_01.xml` | `.pytest_t45_full_01.xml` |
| command | `.venv/Scripts/python.exe -m pytest --junitxml=.pytest_t45_full_01.xml -q` |

### 15.1 Verdict

**Green, with one pre-existing failure that is not T4.5's.**

The `+117` is exactly the new file's test count and nothing else moved. The single
failure is the **same test** in both runs:

```
tests.test_report_line_refs::test_the_report_has_no_new_symbol_mismatches
```

It is the **advisory ratchet** (`assert 24 <= 18`) over the report's own
symbol-mismatch count. The tool prints, of itself, `this section is advisory and does
not change the exit code`; the threshold is frozen at 18 on purpose and is not raised
to make a run green. It is **pre-existing and untouched by T4.5**, established two
independent ways:

1. **Structurally.** T4.5's `cli.py` insertions sit at editor-system lines **10149**
   and **11103** (editor count 12527, grep count 6186). The highest `cli.py` reference
   the report makes is **7011** — every cited address lies *above* the first insertion,
   so no reference could have shifted. `docs/ble001_triage.md` is also **unmodified
   against `HEAD`**, so T4.5 translated no reference either.
2. **Empirically.** `cli.py` was swapped for `git cat-file -p HEAD:ai_assistant/cli.py`
   and the advisory re-run: the count stayed **24** and the flagged list was
   **byte-identical** (`diff` empty). The working-tree state of `cli.py` — the foreign
   uncommitted edit *and* T4.5's own +73 lines — changes **not one flagged reference**.
   `cli.py` was then restored and re-hashed to `8e3ddfab461ebc40…`.

The exit code is not the verdict here either. The run reached `[100%]` and was then
killed by the safe-delete guard during its own temp cleanup
(`{"count":1877,"threshold":50,"scope":"turn"}`), so **no summary line was printed at
all** and the process exited **`1`** after 24 m 58 s. The numbers above come from
`.pytest_t45_full_01.xml`, which was written before the kill and parses cleanly
(2234 testcases, 0 malformed, 0 out-of-range references).

**Correction to an earlier note.** A previous session recorded this ratchet as
"red at 23, caused by the foreign uncommitted `cli.py` edit", and a first draft of
this section tried to explain the delta as "a change of instrument". Neither survives
the record, and the second was my own guess rather than a measurement. What *is*
measured here is narrower and firmer: the standing baseline **before** T4.5 was already
**24** — the project log records `assert 24 <= 18` on **2026-09-21** (work #63: 1934 tests,
1 failure, ratchet 24) and again on the `HEAD` pair for work #65 — and T4.5 leaves it at 24,
shown two ways above. The "23" in the older note predates two *committed*
changes: the report's reference translation and the 2026-09-21 refinement of the advisory
tool (#63, whose new probe-status-token and git-ref false-positive classes are visible
in the output above). Which of the two moved the number is **not** established by
anything measured here, so it is left unattributed rather than guessed.

### 15.2 How to read this project's regressions

The safe-delete guard can kill pytest during its own cleanup, **after** `[100%]`
but **before** the summary line — so a non-zero exit code with a green run is
normal here and the exit code is not the verdict. This run did exactly that:
`[100%]`, then `[SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":1877,"threshold":50,
"scope":"turn"}`, then death — and **no summary line was ever printed**.

Verdicts are therefore read from `--junitxml`. Precisely why that works: pytest's
junit plugin writes the file **once, in `pytest_sessionfinish`**
(`_pytest/junitxml.py:647` → `logfile.write` at `:662`/`:683`), and that hook runs
**before** the temp-directory cleanup finalizers that the guard blocks. So the file
is complete on disk by the time the process dies. It is *not* written incrementally:
a run killed *before* sessionfinish leaves no junit at all, and that absence must be
read as "no measurement", never as "no failures". *(An earlier draft of this section
said "written as the run proceeds"; the plugin source says otherwise, and the
correction is recorded here rather than silently applied.)*

One more caveat about `tools/report_line_refs.py`: it resolves a cited basename only
against **git-tracked** `.py` files (`by_base = tracked_py()`). All four references in
*this* report therefore come back `-> no such file` — three point into
`ai_assistant/candidate/kb.py` and `tests/test_candidate_kb.py`, which are **untracked**,
and one into the venv's `_pytest/junitxml.py`, which is not in the repository at all.
That is a resolution limit, not a broken reference; each was verified by reading the
cited line:

| reference | what is actually there |
|---|---|
| `kb.py:268` | `def merge_claims(` |
| `tests/test_candidate_kb.py:385` | `assert payload["kb_format_version"] == 1` |
| `tests/test_candidate_kb.py:517` | `assert f"added 0, refreshed {len(claims)}" in out` |
| `_pytest/junitxml.py:647` | `def pytest_sessionfinish(self) -> None:` |

An earlier draft of this report cited line 249 of `kb.py` and line 516 of
`tests/test_candidate_kb.py`. Reading the files showed the first sat inside `to_dict()`,
not `merge_claims`, and the second was one line above the substring pin. Both are
corrected above rather than left standing. They are written here *without* the
`file.py:NNNN` form on purpose — the reference counter would otherwise read a corrected
address as a live one, which is the same class of confusion the tool's own
false-positive list warns about.

---

## 16. Limitations

1. **A paraphrase could still be wrong.** The nets catch invented numbers and
   invented technologies, not a subtly wrong rewording. The candidate sees every
   fact with its quote before confirming, which is the actual defence.
2. **The plan can drift mid-interview.** Confirming facts changes the KB, and the
   KB feeds the plan. A resumed session compares a fingerprint and warns, and
   `--rebuild` refreshes the question list while keeping answers and decisions —
   but the questions themselves are frozen at session start, deliberately, so a
   resume lands on the same interview.
3. **The technology net is only as good as its vocabulary.** It uses the resume's
   own skill values. A technology named in a fact but absent from the resume's
   skills is not checked; the verbatim net is what covers that case.
4. **The interview is a CLI, not a conversational agent.** The brief allowed the
   existing agent dialogue interface *if genuinely available*; the only "dialogue"
   in this repo is HH messaging (`test_stage24_full_dialog.py`), which is a
   different thing. So this is the minimal stateful CLI the brief specifies.
5. **`--text` is required for answers beginning with a dash.** An argparse
   limitation of optional positionals, documented in §5.1 rather than papered over.
6. **Unconfirmed facts live only in the session file.** Deleting it loses them.
   They are unconfirmed by definition, so nothing the candidate decided is lost —
   but it is a real property of the design.
7. **No claim is made that the interview is short.** 19 questions is the honest
   consequence of grounding every question in a real finding.

---

## 17. Readiness for T5

T5's plan spec says: master resume contains **all** roles from `resume.md`,
including Avantis and the 2009–2018 block; no claim below `DERIVED_FROM_SOURCE`;
length unbounded. T4.5 gives it:

* **one place to read facts from** — `kb.claims`, where resume claims and interview
  claims sit together, distinguished by `is_interview_sourced` and by `in_source`;
* **an effective-fact rule for revisions** — a confirmed claim carrying
  `evidence.superseded_by` has been revised, so T5 should prefer the claim that
  superseded it;
* **a hard experience/preference boundary** — `is_experience_kind(kind)` tells T5
  which facts may enter a resume at all;
* **answers to the three ambiguities** — once confirmed, `experience_tenure` facts
  carry the candidate's own statement about total tenure and about what
  `years_experience` measures, which is what makes the T3 contradiction
  answerable rather than merely detectable;
* **the gap and the salary** — as decisions, so T5 can state or omit them without
  guessing.

Two things T5 must not assume: that every finding was answered (skips are
recorded, not resolved), and that an interview fact is a resume fact (it is not
until its kind says so).

---

## 18. Conclusion

T4.5 adds the one stage the chain was missing: a way to ask. It reuses the
knowledge base, its statuses, its id scheme and the audit rather than duplicating
any of them; it puts nothing in the knowledge base that the candidate did not
explicitly confirm; it keeps the resume half of the KB byte-identical through a
full run and through a subsequent resume re-extract; it refuses to invent numbers
or technologies; and it consults a paid model only when asked to.

117 new tests, 9 new mutation probes (all CAUGHT), 22 probes in total all with
valid anchors and calibration. The full regression is **2234 tests, 0 errors, and
exactly one failure — the same pre-existing advisory ratchet that also failed the T4
baseline run** (§15.1). **T5 may proceed.**
