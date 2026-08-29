# Stage 30D Spec — `hh-message diagnose` (READ-ONLY diagnostic command)

> Basis: Stage 30C closed at commit `b553633` (56 tests green). This spec is
> self-contained: the implementing agent must not re-research the project.
> Hard constraint: diagnose is a PROBE. No navigation, no click, no send,
> no submit, no mutation, no DB writes, no auto-reply, no env toggles.

## 1. CLI contract

    python -m ai_assistant.cli hh-message diagnose [--cdp-url URL] [--url-substring S]
                                                    [--frame-substrings S1,S2]
                                                    [--json]

- Registered under the existing `hh-message` subparser group next to
  `list` / `preview` / `classify` (`cli.py` ~1943-1957; dispatch ~2123).
- Defaults reuse the existing module constants:
  - `--cdp-url` → `HH_CDP_URL` env or `http://127.0.0.1:9222` (`_DEFAULT_HH_CDP_URL`);
  - `--url-substring` → `hh.ru` (`_DEFAULT_HH_MESSAGES_URL_SUBSTRING`);
  - `--frame-substrings` → `chatik.hh.ru,/chat/` (`_DEFAULT_CHATIK_FRAME_SUBSTRINGS`).
- `--json` switches to the machine-readable payload (§5). Default output is
  human text (§4).
- Exit codes: `0` = probe ran to completion (regardless of findings — the
  verdict is data, not an error); `1` = probe itself could not run
  (CDP endpoint unreachable, WebSocket transport failure, unexpected
  exception). Rationale: `hh-message list` already returns 1 on transport
  failure; diagnose distinguishes "probe failed" from "probe says broken".

## 2. Data collected (checks)

`hh_message_diagnose(cdp_url, url_substring, frame_substrings, evaluate_fn=None,
frame_probe_fn=None, targets=None) -> dict` in `ai_assistant/cli.py`,
returning these keys:

| Key | How obtained | Reused helper |
|---|---|---|
| `cdp_reachable` | GET `<cdp-url>/json/list` (10s timeout) | `_cdp_list_targets(cdp_url)` from `prefill_execute.py` |
| `matching_tabs` | all `type=="page"` targets whose URL contains `url_substring` (url + title each) | inline filter (same predicate as both `make_*_evaluate`) |
| `hh_page_present` | `len(matching_tabs) >= 1` | — |
| `page_url`, `page_title` | first matching target's `url`/`title` | — |
| `page_is_messages` | `/messages|messaging|negotiations/i.test(location.href)` evaluated in the page | same regex as `_DIALOG_LIST_JS` (`hh_message_reply.py`) |
| `frames` | flattened frame tree: `[{frameId, url, matched}]` | `Page.getFrameTree` over the same WebSocket; walking logic mirrors `select_frame_id_by_url` |
| `chatik_frame_found` | any frame matching `frame_substrings` | `select_frame_id_by_url(frame_tree, frame_substrings)` from `prefill_execute.py` |
| `chatik_frame_url` | URL of the matched frame (prefer `/chat/` match, per existing precedence) | same |
| `isolated_world_ok` | `Page.createIsolatedWorld {frameId, grantUniveralAccess: False}` returns non-null `executionContextId`; then a trivial `Runtime.evaluate` of `"1+1"` with `contextId` returns `2` | pattern of `make_isolated_world_evaluate._cdp_evaluate_in_frame` |
| `conversation_dom_ok` | run `_CONVERSATION_JS` in the isolated world; parse JSON without error and no `error` key | `hh_message_reply.fetch_hh_conversation_readonly(evaluate_fn)` |
| `conversation_id` | from the parsed conversation payload | same |
| `composer_present` | from the parsed payload | same |
| `message_count` | `len(messages)` | same |
| `dialogs_visible` | run `_DIALOG_LIST_JS` in the MAIN frame; `len(dialogs)` (0 tolerated — list view only exists on the messages page) | `hh_message_reply.fetch_hh_dialogs_readonly(evaluate_fn)` |
| `errors` | list of per-step error strings (transport, parse, CDP errors) — each step fails soft | — |

Every check is fail-soft: a failed step records a message in `errors` and the
downstream keys become `null`/`false`; the probe always completes and prints a
verdict.

## 3. What diagnose MUST NOT do

No `Page.navigate`/location change, no clicks, no `Input.*`, no composer
interaction, no `Runtime.evaluate` that writes DOM, no DB access
(`init_db` excluded), no env toggles (`HH_AUTO_REPLY_ENABLED` untouched),
no AUTO-path imports (`auto_apply_modes`, `hh_controlled_submit`,
`hh_submission`, `hh_human_submission`, `EmailSendGate`,
`process_auto_reply`, `send_auto_reply`, `confirm_live_send` — all must
remain absent from the new handler; mirror the existing forbid-list tests).
The only CDP methods allowed: `Page.enable`, `Page.getFrameTree`,
`Page.createIsolatedWorld` (`grantUniveralAccess: False`), `Runtime.evaluate`
with read-only expressions (`"1+1"`, `_DIALOG_LIST_JS`, `_CONVERSATION_JS`).

## 4. Human output

```
[hh-message] diagnose (READ-ONLY probe)
cdp:            http://127.0.0.1:9222 — reachable (12 targets)
hh tab:         YES — "Messages | hh.ru" (https://hh.ru/messages/...)
page is messages: YES
frames:         3 (chatik matched: https://chatik.hh.ru/chat/12345)
isolated world: OK (executionContextId acquired, evaluate 1+1 = 2)
conversation DOM: OK — conversation_id=12345, messages=7, composer=YES
dialogs visible: 14
errors:         none
verdict: HEALTHY — preview/classify should work on this tab.
status: READ-ONLY — nothing sent.
```

`verdict` is one of the five states from §6. Failures print the failing line's
value as `NO`/`n/a` plus the error text from `errors`, and end with a one-line
hint (see §6).

## 5. Machine output (`--json`)

Exactly the dict from §2 plus `{"verdict": "...", "checked_at": "<UTC ISO>"}`.
One JSON object, one line per key block is NOT required — print
`json.dumps(payload, indent=2, ensure_ascii=False)`. No file writes.

## 6. Verdict decision table (differentiation contract)

| Condition (evaluated in order) | verdict | hint |
|---|---|---|
| `/json/list` unreachable or WS connect fails | `CDP_UNAVAILABLE` | "start Chrome with --remote-debugging-port=9222" |
| `matching_tabs == 0` | `HH_NOT_OPEN` | "open hh.ru in the CDP browser" |
| a tab matches but its URL is not the messages section (`page_is_messages` false) | `HH_WRONG_PAGE` | "open the hh.ru messages page (dialog list) in the tab" |
| `chatik_frame_found == false` | `CHATIK_FRAME_ABSENT` | "open a specific conversation in the hh.ru tab" |
| frame matched but `isolated_world_ok == false` | `ISOLATED_WORLD_UNAVAILABLE` | "chatik frame exists but CDP could not create an isolated world" |
| isolated world OK but `_CONVERSATION_JS` errored / unparseable | `CONVERSATION_DOM_INACCESSIBLE` | "chatik iframe reachable but message DOM could not be read" |
| DOM read OK, `message_count == 0` | `NO_MESSAGES` | "conversation is open but empty (or DOM selectors changed — compare with Stage 24 evidence)" |
| everything OK | `HEALTHY` | — |

This table is the acceptance contract: the tests in §7 pin each row.

## 7. Tests (`tests/test_stage30d_diagnose.py`, fakes only — no live CDP)

Follow the existing Stage 30C fake-injection style (`evaluate_fn` /
`_run_in_frame` seams). One test per verdict row plus safety:

1. `test_cdp_unreachable_verdict` — targets fetcher raises → `CDP_UNAVAILABLE`, exit 1, no `Page.*` calls.
2. `test_hh_not_open_verdict` — targets exist, none match substring → `HH_NOT_OPEN`.
3. `test_hh_wrong_page_verdict` — matching tab, main-frame evaluate returns `pageIsMessages: false` → `HH_WRONG_PAGE`.
4. `test_chatik_frame_absent_verdict` — frame tree has no matching frame → `CHATIK_FRAME_ABSENT`.
5. `test_isolated_world_unavailable_verdict` — frame found, world creation returns no contextId → `ISOLATED_WORLD_UNAVAILABLE`.
6. `test_conversation_dom_inaccessible_verdict` — world OK, `_CONVERSATION_JS` result is not JSON → `CONVERSATION_DOM_INACCESSIBLE`.
7. `test_no_messages_verdict` — parsed payload with `messages: []` (i.e. `_EMPTY_CONVERSATION_JSON` path) → `NO_MESSAGES`.
8. `test_healthy_verdict` — full fake happy path → `HEALTHY`, message_count reported.
9. `test_json_flag_payload_contract` — `--json` prints a dict containing every §2 key; `verdict` present; no extra file writes.
10. `test_diagnose_never_sends_or_mutates` — forbid-list scan of `cli.py` unchanged (reuse pattern from `test_stage30c_cli_wiring.py:247`); fake evaluate_fn asserts the only evaluated expressions are `"1+1"`, the dialogs IIFE, and the conversation IIFE; no `Page.navigate` in handler source.
11. `test_fail_soft_records_errors` — mid-probe exception lands in `errors[]`, probe still returns a verdict (never raises to CLI).
12. `test_diagnose_human_output_lines` — text mode prints each §4 label and the `status: READ-ONLY` trailer.

## 8. Helpers: reuse vs create

**Reuse (do NOT duplicate):**
- `_cdp_list_targets(cdp_url)` — `prefill_execute.py` (target discovery).
- `select_frame_id_by_url(frame_tree, substrings)` — `prefill_execute.py` (chatik frame location, `/chat/` precedence).
- `make_cdp_evaluate` / `make_isolated_world_evaluate` — only for the main-frame and chatik read paths via `_resolve_hh_evaluate` / `_resolve_chatik_evaluate` when `evaluate_fn` is not injected.
- `fetch_hh_dialogs_readonly`, `fetch_hh_conversation_readonly`, `_DIALOG_LIST_JS`, `_CONVERSATION_JS` — `hh_message_reply.py` (unchanged).
- CLI registration/dispatch patterns + `_DEFAULT_*` constants from `cli.py`.

**Create (new, minimal):**
- `hh_message_diagnose(...)` handler in `cli.py` with two injectable seams: `evaluate_fn` (single fake for both transports, test decides what each expression returns) and `targets` (injectable `/json/list` result, replaces the live `_cdp_list_targets` call in tests).
- A small non-async frame-tree/world prober reusing the WebSocket call pattern of `_cdp_evaluate_in_frame` — or, preferably, route the whole probe through the existing transports and keep the new CDP surface to zero. Prefer the zero-new-CDP variant: obtain `frames`/`world` facts by running tiny read-only expressions and by extending nothing (`chatik_frame_found` is derivable from whether the isolated transport returned `_EMPTY_CONVERSATION_JSON` vs real data; add an explicit `--frame-substrings`-driven frame listing only if the transports cannot express it — the implementing agent may add ONE private helper `_probe_frames(cdp_url, ws_url, substrings)` in `prefill_execute.py` if strictly needed, marked Stage 30D).
- No new module, no new dependency, no schema change.

## 9. Degradation rules (fail-soft)

Each step depends only on its predecessors; any failure → record in
`errors[]`, set dependent keys to `null`/`false`, continue to the next
independent check where possible, always reach the verdict table. The probe
NEVER raises to the CLI except for probe-level failure (§1 exit 1: CDP
endpoint unreachable counts as a verdict `CDP_UNAVAILABLE` with exit 0 when
reached through fail-soft — exit 1 is reserved for unexpected exceptions).

## 10. Implementation order (for the implementing agent)

1. `hh_message_diagnose` + verdict logic in `cli.py` (pure function of injected fakes — test it without any CDP).
2. argparse + dispatch wiring mirroring `hh-message classify`.
3. Wire real transports via `_resolve_hh_evaluate` / `_resolve_chatik_evaluate`.
4. Tests §7, all fakes; `pytest tests/test_stage30d_diagnose.py -q` green.
5. Do NOT run against the live CDP 9222 browser and do NOT commit unless the operator asks — live verification is a separate, operator-approved step.
