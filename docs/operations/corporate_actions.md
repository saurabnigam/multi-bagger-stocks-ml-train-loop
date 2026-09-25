# Operator runbook: suspected corporate actions (D3 / T2)

Context: `quant.data.actions.detect` (run during `data capture` / `run monthly`) flags an
unexplained one-day total-return jump outside `[1/1.40, 1.40]` as a `suspected` corporate
action (MASTER_SPEC 2.3, 4.3). Until a human resolves it, `PriceStore.corporate_actions`
truncates that security's history at the ex-date -- the jump is never read as a real or
zero return. `data actions-list` / `data actions-resolve` are the operator path from
"flagged and truncated" to "reviewed and restored".

## 1. See what is truncated

```bash
python3 -m quant data actions-list --db-path quant.db
```

Shows one row per **unresolved** suspected action: symbol, ISIN, ex-date, the gross
factor from the evidenced jump, the evidence's observation time, and whether the ex-date
falls inside the trailing 13 months of that security's latest price bar (`yes` means it
is still distorting the current live cohort, not just historical replays -- review those
first).

```bash
python3 -m quant data actions-list --db-path quant.db --all       # include already-resolved rows
python3 -m quant data actions-list --db-path quant.db --json      # machine-readable
```

## 2. Look up the real cause

Pull the company's filing (BSE/NSE announcement, scheme of arrangement, board notice) for
the flagged ex-date and decide which of these it actually was:

| What the filing shows | `--kind` | Value to pass |
|---|---|---|
| Demerger / scheme of arrangement moved value to another entity | `demerger` or `scheme` | `--factor F` (pre-close / post-close, so `close_before / close_after`) |
| Rights issue | `rights` | `--factor F` |
| Some other value transfer not covered above | `manual_adj` | `--factor F` |
| Vendor missed a split or bonus issue | `split` or `bonus` | `--ratio R` (new shares per old share) |
| No corporate action at all -- the move was a real trading loss/gain | `genuine` | (no factor/ratio; stored as `manual_adj` with `adj_factor = 1.0`) |

## 3. Resolve it

```bash
python3 -m quant data actions-resolve --db-path quant.db \
  --isin INE000A01023 --ex-date 2025-10-14 \
  --kind demerger --factor 1.6667 \
  --evidence "BSE announcement 2025-10-10: scheme of arrangement, record date 2025-10-14" \
  --actor-kind human --by human:<your-name>
```

`--symbol TATAMOTORS` works in place of `--isin` if that is easier to look up.

This must run with `--actor-kind human`; anything else is refused (`Refused(governance)`)
-- a wrong value-transfer factor silently corrupts total return for every downstream
evaluation, so it is never LLM-provisional the way most Tier-1 data fixes are (MASTER_SPEC
9.3). In one journaled run it: creates a Tier-1 `data_fix` decision at status `approved`
(subject = the ISIN, your evidence text in `evidence_refs_json`), writes its ADR under
`knowledge/decisions/` (so `quant kb check` stays clean), and records the approved
`corporate_actions` row. From then on, `PriceStore` uses that factor for TRI and
split-consistent close, and the security drops out of `data actions-list`'s default view.

**A genuine move** (no corporate action, the loss/gain was real):

```bash
python3 -m quant data actions-resolve --db-path quant.db \
  --isin INE000A01023 --ex-date 2025-10-14 \
  --kind genuine \
  --evidence "Reviewed Q2 filing: no scheme/demerger; operating loss confirmed" \
  --actor-kind human --by human:<your-name>
```

This stores `manual_adj` with `adj_factor = 1.0` -- it stops the truncation without
changing the observed return.

### Guardrails

- **Refused if already resolved.** `corporate_actions` is append-only; a second
  `actions-resolve` for the same `(security, ex-date)` is refused with the existing
  decision ID rather than silently creating a conflicting row.
- **Factor sanity.** The factor/ratio must be in `(0, 20]`. For anything other than
  `genuine`, if `gross_factor x factor` still lies outside `[1/1.4, 1.4]` -- i.e. your
  chosen factor does not actually explain the evidenced jump -- the command prints a
  warning and refuses unless you pass `--force`. Re-check the filing before forcing.
- **Idempotent detection.** Once resolved, `actions.detect` will not re-flag the same
  `(security, ex-date)` on a later capture.
