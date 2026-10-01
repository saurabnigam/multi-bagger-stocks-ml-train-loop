# Operator runbook: evidence triage

Triage helps you resolve a suspected corporate action faster and with better evidence. It lists the
anomalies, archives the documents you attach (and a few automatic sources), runs a fixed rule set, and writes
one proposal per anomaly with its evidence, its counter-evidence and the exact `data actions-resolve`
command it would support. **It never resolves anything.** You run `data actions-resolve` yourself, with
`--actor-kind human`. The engine runs with no LLM. An LLM agent is optional and can only add a second proposal
that the engine re-checks.

Read this together with [corporate_actions.md](corporate_actions.md) (what a resolution does and its
guardrails) and [triage_sources.md](triage_sources.md) (what each source may fetch and what a silent source
means). Code: `quant/triage/` (engine), `quant/commands/triage.py` (this CLI).

## 1. Ground rules

| Rule | What it means for you |
|---|---|
| **The cohort window closes at the cutoff.** | Cohort `as_of` D sees only what was captured at or before D 23:59:59.999999 IST (= D 18:29:59.999999 UTC). `collect`, `attach`, `build` and `submit` refuse with exit 3 once the clock is past that instant, before any network call or write. Triage it for the next cohort instead. This is the point-in-time rule working, not a bug. |
| **`captured_at` is the moment you ran the command.** | You cannot back-date evidence. `--published-at` is what the source claims, and it must not be after the moment of capture. It is never defaulted: an undated document is context only and cannot support a decisive call. |
| **`list` and `show` are read-only.** | They open a read-only database connection and a read-only price store, no run is recorded, nothing is written. |
| **`--dry-run` writes nothing.** | `collect --dry-run` also makes no network call; it prints the plan. `build --dry-run` prints `would create` or `unchanged` per anomaly. |
| **Nothing is fetched from IRDAI, NSE or BSE.** | IRDAI's robots.txt disallows every agent; NSE and BSE terms are unverified. You read those documents yourself and attach them. `attach --url` stores the link and never opens it. |
| **`--actor-kind` is a label, not authentication** (MASTER_SPEC 9.3). | The code refuses the wrong label for each command, which stops mistakes and prompt-driven misuse. A hostile runtime can lie, so keep any agent away from the shell where you type `--actor-kind human`. |
| **Rebuilding is idempotent.** | With no new evidence and no new bars, `build` reproduces the same pack hash and writes nothing, not even a run row. A new proposal row (superseding the old one) is written only when the payload changes. |

Exit codes: `0` ok, `1` bad invocation or another error, `2` blocked, `3` refused (window closed, wrong actor,
validation failed, wrong `--confirm`).

## 2. Commands

| Command | Who | What it does |
|---|---|---|
| `triage list --as-of D [--since DATE] [--json]` | anyone | Lists the anomalies triage covers for cohort D with their status: `new`, `proposed`, `wait` and `rejected`. `rejected` means you rejected the latest proposal: nothing was resolved, the anomaly is still truncated and still open, so it stays on the list until you resolve it or a rebuild with new evidence proposes again. An anomaly you have resolved leaves the list (`resolved` shows only for a proposal you approved after this cohort's cutoff). Warns if the window has closed. |
| `triage collect --as-of D (--isin I --ex-date X \| --all) [--dry-run]` | human or system | Asks Yahoo for corporate actions and news per anomaly (and polls any enabled feed), archives each answer as evidence. Source status (`ok`, `empty`, `error`, `skipped`) is printed and kept in the run notes. Refused for llm actors. |
| `triage attach --as-of D --isin I --ex-date X --title T (--file PATH \| --url URL) --source-class C --relevance R [--published-at DATE] [--excerpt TEXT] [--field k=v ...] [--dry-run]` | human; llm with `--url` only | Archives one document as evidence. `--file` copies the body to `data/archive/triage_private/` (git-ignored, content-addressed). `--field` stores typed values such as `prev_close=660.75`. |
| `triage build --as-of D (--isin I --ex-date X \| --all) [--dry-run]` | human or system | Pure: features from the stores, admissible evidence, rules, pack, proposal, sheet. No network. Refused for llm actors: the rows it writes are engine rows, which the validator never sees. |
| `triage show (--proposal-id ID \| --isin I --ex-date X) [--as-of D] [--json]` | anyone | Prints the lookalike warning and counter-evidence first, then evidence, then the rules and agent proposals side by side, then the resolve command as text. With `--as-of D` it warns when the rules proposal was built for another cohort; it also warns when the agent proposal and the rules proposal belong to different cohorts. |
| `triage submit --as-of D --file proposal.json --actor-kind llm --by llm:MODEL [--dry-run]` | llm only | Validates an agent proposal (citations, quotes, numbers, timestamps, recomputed factor) against the pack and rules proposal built for cohort D. A pack or rules proposal from another cohort is refused (`V1:binding`, "build for this cohort first"): its features and evidence are another cutoff's. Any failure prints every problem, exits 3 and writes nothing. |
| `triage reject --proposal-id ID --reason CODE` | human only | Marks a proposal `rejected`. The anomaly stays unresolved. |
| `data actions-resolve ... --proposal-id ID --confirm SHA8` | human only | The existing command, plus a binding to one proposal version. See section 4. |

`--source-class` is one of `exchange`, `regulator`, `company`, `news`, `other`. `--relevance` is `entity`
(the document is about this company), `sector` (it is about the company's sector) or `context`.
`--published-at` is `YYYY-MM-DD` or a timestamp with a timezone. An excerpt is at most 50 words.

`attach` checks whether the title, the excerpt and (for a text file) the body name the company, using whole-word
matching on the company name and the NSE symbol. The result is stored as `entity_match`, not taken from you. If you
declare `--relevance entity` and the text never names the company, the engine treats the document as context, and
`attach` warns you. The rules only use a document as evidence about the company when `entity_match` is true.

Suggested `--reason` codes for `reject` (any lower-case code of letters, digits and underscores is accepted; the
evaluation groups by code): `wrong_label`, `wrong_factor`, `bad_evidence`, `stale`, `duplicate`, `other`.

## 3. The cohort evening

The October cohort is dated **2026-10-30**. 2026-10-31 is a Saturday, so the cohort takes the last trading day.
Times are IST; the clock times are a suggested rhythm, only the cutoff is fixed.

```
 Fri 2026-10-30  as_of 2026-10-30

 18:00  python3 -m quant data capture
        prices now cover the ex-dates

 18:30  python3 -m quant triage list    --as-of 2026-10-30
        python3 -m quant triage collect --as-of 2026-10-30 --all --actor-kind human --by human:<you>
        read the documents; for each anomaly:
        python3 -m quant triage attach  --as-of 2026-10-30 --isin I --ex-date X ...
        python3 -m quant triage build   --as-of 2026-10-30 --all

 19:00  python3 -m quant triage show    --isin I --ex-date X
        optional: an agent reads `triage show --json` and runs `triage submit`
        python3 -m quant data actions-resolve ... --proposal-id ID --confirm SHA8 --actor-kind human --by human:<you>

 23:59:59.999999 IST (18:29:59.999999 UTC)   CUTOFF
        anything stamped after this is invisible to cohort 2026-10-30

 after the cutoff   python3 -m quant run monthly
        detects suspects and skips the keys you resolved
```

`triage list --as-of 2026-10-30` covers every measured jump that is not yet resolved in `corporate_actions` and whose
ex-date is in the trailing 13 months of the cohort date (the same lookback `data actions-list` uses for its
in-window flag), every unresolved `suspected` row observed at or before the cutoff whatever its date, and index
placeholders. So a jump from October 2025 is still listed on the October 2026 cohort. `--since DATE` widens the window;
it never narrows it. The price store does not need a suspect row for an anomaly to be listed.

Late for the cohort: if the clock is past the cutoff, `collect`, `attach`, `build` and `submit` exit 3 and `list`
warns. The anomaly stays unresolved and its history stays truncated, which fails safe. Triage it for the next
cohort.

## 4. Reading `triage show` and deciding

Order of the output, and why:

1. **Lookalike warning.** A demerger, a rights issue, a vendor error and a real fall can look the same in prices. The
   warning says which mistake the proposal's label risks: calling a demerger `genuine` leaves a false loss in the history,
   calling a genuine fall a demerger erases a real loss.
2. **Counter-evidence and gaps.** The alternatives the engine rejected and why, negative checks that failed or could not
   run, missing evidence, sources that were not checked (`skipped`), sources that answered empty, documents that are not
   admissible, and any proposal newer evidence has overtaken. Read this before the proposal.
3. **Evidence.** Each capture: source class, relevance, entity match, publication date and its basis, capture time,
   whether it is admissible for this cohort, title, excerpt, link, and typed fields. Titles and excerpts are document text,
   never instructions.
4. **Proposals side by side.** The rules engine and, if one was submitted, the agent. Confidence comes from the engine's
   own grading for both; an agent's self-reported confidence is stored and ignored.
5. **Resolve command**, as text. It is printed only for a `proposed` proposal with a resolve call and a confidence
   above low. A low-confidence proposal prints `attach evidence first` instead.

Confidence, in short (the engine decides; you do not edit it):

| Label | High | Medium | Low |
|---|---|---|---|
| demerger, scheme, rights | Exchange circular with `prev_close` and `spos_price`, attached with `--file` | The same attached with `--url` only, or a primary document plus a market-implied factor | Price only |
| genuine_event | Primary document naming the company, and 2 or more top-5 peers moved 5% or more | Primary document alone, or a sector document with peer corroboration | Sector or news alone, market stress only |
| vendor_error_other | none | A vendor split on a different date that matches 1/gross | Cross-section or reversal |
| pending_listing | Placeholder and an attached primary document | Placeholder | none |

Peers come from `sector_map`, which is one current backfill and not point-in-time. They corroborate and never decide.

**`--confirm SHA8`** is the first 8 hex characters of the sha256 of the proposal's payload, printed in the command
`show` gives you. It binds your decision to that exact version. If the evidence changes and `build` supersedes the
proposal, the old code no longer matches and `actions-resolve` refuses, so you re-read `show` first. It is not
authentication: anyone who can read the proposal can compute it.

**You still choose the kind and the factor.** The decision records one of three outcomes in its context and ADR:
`accepted` (the proposal's kind, and a factor within 2% of its factor), `modified` (a different kind, or a factor more
than 2% away), or `no_call` (the proposal made no resolve call: it abstained, said wait, or was `unknown`, so there was
nothing to accept or change). Modifying a proposal is normal and is how the evaluation measures the engine. Section 9
says how each outcome is counted.

**`needs_force`.** If `gross x factor` would leave the series outside `[1/1.4, 1.4]`, `actions-resolve` refuses unless
you add `--force`. `show` never prints `--force`. That call is yours alone: re-read the document first.

Resolving (`data actions-resolve`) sets the proposal to `approved` and links the decision (`decisions.proposal_id`). A
wrong `--confirm`, a proposal that is not `proposed`, or a proposal for another security or date is refused and nothing is
written. `--confirm` without `--proposal-id` is refused.

## 5. Worked example: POLICYBZR (INE417T01026), ex-date 2026-09-24

Policybazaar fell from 1886.30 to 1210.00 on 2026-09-24 after an IRDAI consultation paper on insurance distribution
(press material dated 22 to 23 September 2026, comments open until 25 October 2026; secondary sources only).

```
gross factor = 1210.00 / 1886.30 = 0.6415      outside [0.714, 1.4]: a jump
```

**Not triage-able for the September cohort any more.** The cutoff for as_of 2026-09-30 passed at 2026-09-30
23:59:59.999999 IST. From 2026-10-01, every `collect`, `attach`, `build` and `submit` for that cohort exits 3, and the
suspect stays truncated. Triage it for the **2026-10-30** cohort, where its ex-date is inside the 13-month window.

Steps on 2026-10-30 (the IRDAI paper is read and attached by you; the engine never fetches irdai.gov.in):

```bash
python3 -m quant triage list --as-of 2026-10-30            # POLICYBZR appears with status new
python3 -m quant triage collect --as-of 2026-10-30 --isin INE417T01026 --ex-date 2026-09-24 \
    --actor-kind human --by human:<you>
python3 -m quant triage attach --as-of 2026-10-30 --isin INE417T01026 --ex-date 2026-09-24 \
    --title "IRDAI consultation paper: recalibrating the economics of insurance distribution" \
    --url "<link to the IRDAI paper>" --published-at 2026-09-23 \
    --source-class regulator --relevance sector \
    --excerpt "<up to 50 words copied from the paper>" \
    --actor-kind human --by human:<you>
python3 -m quant triage build --as-of 2026-10-30 --isin INE417T01026 --ex-date 2026-09-24
python3 -m quant triage show  --isin INE417T01026 --ex-date 2026-09-24
```

What the design expects (design expectations, not a run against the real database; the peer counts are assumptions):

- **Before the attach:** prices alone give `unknown`, low confidence, and `show` says `attach evidence first`. The market
  did not move 4% or more that day (assumption), so the market-stress rule does not fire.
- **After the attach:** rule R7 fires if the document is dated within 5 days before the ex-date (2026-09-23 is) and no
  corporate-action wording, vendor artifact, placeholder or non-EQ series argues against it (the checks are listed below). The label is `genuine_event`,
  `resolve_kind genuine`. The paper does not name the company, so you declare `--relevance sector` and the evidence level is
  the sector level. With at least 2 of the top-5 same-sector peers down 5% or more (HDFC Life fell about 7%; the rest is
  an assumption) the confidence is **medium**; with fewer it is **low** and no resolve command is printed.
- **Not high.** The regulator paper does not name the company, so entity relevance cannot be verified by machine. Medium
  plus your own check is the honest output.
- **You decide.** If you agree it was a real move:

```bash
python3 -m quant data actions-resolve --isin INE417T01026 --ex-date 2026-09-24 --kind genuine \
    --evidence "IRDAI consultation paper 2026-09-23 on distribution commissions; no scheme or demerger; operating re-rating" \
    --proposal-id PT-2026-10-30-001 --confirm <the 8 hex from show> --actor-kind human --by human:<you>
```

`genuine` stores `manual_adj` with `adj_factor = 1.0`: it ends the truncation without changing the observed return.

## 6. Worked example: TMPV (INE155A01022), ex-date 2025-10-14

Tata Motors' passenger-vehicle demerger. The evaluation set records a gross factor of 0.5985, so the market-implied
factor is 1/0.5985 = 1.6708. That is the older recommendation; the exchange-derived factor below replaces it (the
verification report, section 9, gives the reason). TMPV is still unresolved in the real database, so on the 2026-10-30 cohort it is listed
(the ex-date is inside the 13-month window) and its history is truncated until you resolve it.

You attach the exchange circular for the special pre-open session. It gives the previous close and the special pre-open
price (SPOS). Use `--file` so the body is stored and the grade can reach high:

```bash
python3 -m quant triage attach --as-of 2026-10-30 --isin INE155A01022 --ex-date 2025-10-14 \
    --title "TMPV (Tata Motors): special pre-open session for the demerger (NSE circular)" \
    --file "<path to the circular as text or PDF>" --published-at 2025-10-10 \
    --source-class exchange --relevance entity \
    --excerpt "<up to 50 words: previous close and special pre-open price>" \
    --field prev_close=660.75 --field spos_price=400 --field action_type=demerger \
    --actor-kind human --by human:<you>
python3 -m quant triage build --as-of 2026-10-30 --isin INE155A01022 --ex-date 2025-10-14
python3 -m quant triage show  --isin INE155A01022 --ex-date 2025-10-14
```

Check that `attach` prints `entity_match=True` before you rely on the grade. Rule R2 fires only for a document that
names the company as a whole token: the stored company name or the symbol `TMPV`. The symbol is in the title above for
that reason. If `attach` prints `entity_match=False`, put the symbol in the title or excerpt and attach again.

```
exchange-derived factor = prev_close / spos_price = 660.75 / 400.00 = 1.651875
market-implied 1/gross  = 1.6708     (superseded alternative: +1.1% against the exchange-derived figure)
```

Rule R2 fires: label `demerger`, factor 1.651875, basis `exchange_spos`, **high** (the body came from `--file`; from
`--url` alone it would be medium). `show` lists the market-implied 1.6708 as an alternative with its gap. Without the
circular, a primary document that mentions the demerger gives rule R3: medium, factor 1.6708, declared uncertainty 12%
(the measured gaps to exchange-derived factors were +1.1% for TMPV, +6.6% for VEDL and +9.1% for ABFRL). You then resolve with the factor you
trust, which may differ from the proposal (it is logged as `modified` when it is more than 2% away):

```bash
python3 -m quant data actions-resolve --isin INE155A01022 --ex-date 2025-10-14 --kind demerger --factor 1.651875 \
    --evidence "NSE circular 2025-10-10: previous close 660.75, special pre-open price 400.00" \
    --proposal-id PT-2026-10-30-002 --confirm <the 8 hex from show> --actor-kind human --by human:<you>
python3 -m quant data actions-list --all        # the row now shows as resolved
```

The typed fields must match the circular. The engine trusts `--field` values that you type, and cross-checks nothing
against the file body, so read the numbers twice.

## 7. Where things live

| Item | Location | Committed? |
|---|---|---|
| Evidence envelope (provenance, title, `published_at`, typed fields, body hash) | `data/archive/triage/<sha256>.json`, one `captures` row (`kind='triage_doc'`) | `data/archive/` is git-ignored; the `captures` row and hash are in the ledger |
| Attached body (`--file`) | `data/archive/triage_private/<sha256>` | No: git-ignored |
| Pack (features, evidence summary, `source_status`) | `data/triage/packs/<proposal_id>.json`, hash stored in the proposal | Yes |
| Proposal | `proposals` row, `kind='other'`, `rule_id` `TRIAGE_RULES_V1` (engine) or `TRIAGE_AGENT_V1` (agent) | In the state database |
| Sheet (a printable copy of `show`) | `data/triage/sheets/<proposal_id>.md`; `show` reports a missing one and `build` rebuilds it | Yes |
| Source status and reject reasons | `runs.notes_json` of the triage run, and a `REJECT_<code>` flag on the proposal | In the state database |

Packs and sheets are committed, and they carry text from documents: up to 50 words of each attached document's
excerpt, its title, and the headlines Yahoo returned. The attached document body stays out of git; its excerpt does not.
Do not attach an excerpt you would not commit. `triage show` and the sheets strip terminal escape sequences and control
characters from that text and fold line breaks, so a hostile title cannot recolour your terminal or forge a heading.
Packs and sheets are written after the database commit, never inside the transaction, so a failed run leaves no file that
names a proposal that does not exist.

No table and no config key was added. Triage proposals cannot be approved through `kb approve` (it refuses any
`TRIAGE_` proposal for every actor): the only path is `data actions-resolve --proposal-id` by a human. `kb reject` refuses
them too, for every actor kind: a rejection goes through `triage reject --reason CODE`, which only a human may run and which
feeds the eval. An llm actor is also
refused by `prices action-add`, `prices action-clear` and `data actions-resolve`.

## 8. When something looks wrong

| You see | Meaning | Do |
|---|---|---|
| Exit 3 `triage_window_closed` | The clock is past the cutoff of `--as-of`. | Triage for the next cohort. |
| `not checked` or `answered with nothing` in counter-evidence | A source said nothing. Yahoo often returns an empty table for a ticker it cannot read. | Treat as unknown. Attach the document yourself. |
| `entity_match=False` on an `entity` document | The text never names the company (a wrong-company headline, or a regulator paper). | Re-attach with `--relevance sector` if it is sector-wide, or find a document that names the company. |
| `N admissible capture(s) are newer than the rules proposal` (a `!` line) | You attached after the last build. | Run `triage build` again, then `show`. |
| `pack file is hash mismatch` or `missing` | The pack file was edited or deleted. | Rebuild before trusting the features. |
| `DISAGREES_WITH_ENGINE` flag | The agent's label differs from the rules engine's. | Read both; the agent's evidence is checked, its opinion is not. |
| `needs_force` | The factor leaves the series outside the plausibility band. | Re-read the document; `--force` is your decision. |
| `actions-resolve` refuses `confirm_mismatch` | The proposal changed after you read it. | `triage show` again and use the new code. |

## 9. Reading the outcomes: `scripts/triage_eval.py live-report`

`live-report` reads proposals and human decisions from a **copy** of the state database (opened read-only and
immutable, so it writes nothing, not even a `-wal` file) and reports **per producer**: `engine` (the rules) and `llm`
(the optional agent). Nothing is pooled, because the engine's accept rate says nothing about the agent's.

```bash
python3 scripts/triage_eval.py live-report --db <path to a copy of the state database> --json
```

| Outcome | What happened | In the accept and override rates? |
|---|---|---|
| `accepted` | You resolved the anomaly through this proposal and kept its kind and factor (within 2%). | Yes, as accepted. |
| `modified` | You resolved through this proposal but changed the kind or the factor by more than 2%. | Yes, as an override. |
| `rejected` | You rejected this proposal (`triage reject`). | Yes, as an override. |
| `no_call` | You resolved through this proposal, but it made no resolve call (abstain, wait, `unknown`). There was nothing to accept or override. | **No.** Counted on its own line. |
| `pending` | The anomaly is still open. | No. |
| `overtaken` | You resolved the anomaly through the other producer's proposal; this one was not the one you confirmed. | No. |

`accept_rate` is accepted over accepted + modified + rejected. `override_rate` is modified + rejected over the same
total. `no_call`, `pending` and `overtaken` are in neither the numerator nor the denominator. A rate with no
denominator is `null`: unknown, not 0%.

Two more per-producer numbers, both read against your final resolution of the anomaly:

- `dangerous_errors`: a proposal that made a call and was dangerous against what you finally resolved (for example a
  `demerger` at medium or high on a move you resolved as `genuine`, or a factor applied on a vendor or genuine case).
  An anomaly you have not resolved yet is counted as `unresolved`, not as safe. You may have rejected the proposal
  before it did harm; it still counts, because the proposal was wrong.
- `added_value_on_engine_abstained` (`llm` only): on anomalies where the rules abstained, how many calls the agent made
  and how often you resolved as it proposed. The engine has no such number: it cannot add value on its own abstentions.

These figures are anchored: you saw the proposals before you decided, so agreement flatters both producers. Treat them as
advisory until about 30 decided calls per producer.

## 10. Agent documents

The agent contract, prompt and proposal schema are in `docs/operations/triage_agent_contract.md`,
`docs/operations/triage_agent_prompt.md` and `docs/operations/triage_proposal.schema.json`. The contract lists the exact
JSON paths of the pack (`pack.data`) and its hash (`pack.sha256`) in `triage show --json`. The `llm_allowed_kinds`
setting in `config/quant.toml` lists `data_fix` and `clear_ca_flag`, but no code reads it; the refusals above are code,
not configuration.

## 11. What blocks a `genuine_event` call (the negative checks)

The engine calls a move `genuine_event` from a dated document (rule R7) only when every check below reports `none_found`.
The agent validator runs the same checks (V7) over every admissible document, cited or not, and ignores the agent's own list.
A check can also report `unknown` (the engine could not look), and `unknown` blocks the call just as `found` does.

| Check | `found` when | `unknown` when |
|---|---|---|
| `demerger_keyword_in_docs` | any attached or collected document says demerger, scheme of arrangement, spin-off, record date, split, sub-division, bonus, rights, entitlement, ex-bonus or ex-split (whole words), or carries a typed `action_type` other than `genuine`. The title, headline, excerpt **and the archived body** are read (the first 500,000 characters). | a capture could not be read, or a document names a body that cannot be read now: no archive directory, file missing, altered, or not UTF-8 text (a scanned PDF). Attach a UTF-8 text copy of the document (or a `--url` link, which has no body), then build again; an `--excerpt` does not clear it. |
| `vendor_split_present` | the vendor records a split near the ex-date | the split feature is missing |
| `placeholder_present` | an index placeholder points at the security | the placeholder feature is missing |
| `series_not_eq` | the security trades in a series other than EQ | the series is missing |
| `cross_section_jumps_present` | 3 or more securities jumped on the same date while the market median moved under 2% (rule R5's condition) | the count or the market move is missing |
| `reversal_pair_present` | the same security jumped the opposite way within 7 sessions, before or after (rule R6's condition) | the gross factor is unknown |
| `engine_rule_r1_r4` | the rules engine matched a placeholder, SPOS, demerger-document or vendor-split rule | there is no engine proposal |

The two vendor-artifact checks exist so that a dated headline, or a link an agent attached, cannot hide a vendor error: five
unrelated jumps on one quiet day stay a vendor artifact whatever documents exist. Day 0 cannot see the forward half of a
reversal (the next bars do not exist yet), so that half is not required to be known; the rules re-check on the next session.
The market-stress rule R8 keeps its original blockers and ignores the two vendor-artifact checks.
