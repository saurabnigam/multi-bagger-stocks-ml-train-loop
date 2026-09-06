# Test and Verification Plan — V2 Quant Engine

This plan says how the system is proven, not merely built. It is executed by WS11 at sign-off and re-executed by `scripts/signoff.sh` every month. Every workstream doc's §9 lists its own tests; this document defines the shared fixtures, the cross-cutting tests, the end-to-end scenario, the migration acceptance on the real database, the runtime budget and the sign-off checklist.

## 1. Test pyramid

```
level        what                                                     where                            count target   runtime
unit         pure functions: stats, standardise, learn, costs,        tests/unit/                      >= 110         < 40 s
             calendar, units, parsers, gates, criteria
property     invariants that must hold for all inputs: embargo,       tests/property/                  >= 6           < 10 s
             weights sum/bounds, TRI invariance under actions,
             gaussian rank moments, no look-ahead in FactorInputs
leakage      T1–T10 on the synthetic world                           tests/leakage/                   10             < 20 s
integration  tmp DB, synthetic world: monthly loop, three-month       tests/integration/               >= 12          < 60 s
             knowledge scenario, migration on the fixture DB
network      live Yahoo/NSE checks pinned to observed values          @pytest.mark.network             ~8            opt-in
real-data    migration attribution on the real quant_engine.db;       QUANT_LEGACY_REAL=1              3             opt-in
             first monthly run                                        (sign-off only)
```

`python -m pytest -q` must be green offline in under 90 seconds. Nothing in the default run touches the network or the real databases.

## 2. Shared fixtures

### 2.1 The synthetic world (`tests/synthetic.py::make_world`, owned by WS00)

```
securities    60, ids 1..60 ; ISIN-like keys 'INE0000000%02d' ; symbols 'SYN%02d'
groups        6 sector groups of 10 ; group 'G6' has only 4 members after month 30 (tests the merge rule)
months        48 month-ends from 2022-01-31 ; ~21 weekday trading days each ; no holidays
prices        daily log returns = 0.3*market + 0.3*group + idiosyncratic ; close_raw starts at 100..1000 ; volume lognormal ; ADV spans buckets A–D
planted       factor 'plant_mom' with Spearman ~0.10 against the NEXT 3-month sector-relative return (returns are built from the signal) ;
              a second factor 'plant_noise' with 0 ; the composite's EW IC therefore has a known expected sign
events        security 5: 6:1 split on 2023-06-15 (close_raw / 6, split_ratio 6.0)
              security 7: Rs 100 dividend on 2023-09-20
              security 9: reclassified from G1 to G2 at 2024-07-31 (sector_map rows)
              security 11: 'Total Assets' missing for FY2024
              security 13: delisted at 2025-03-15 (prices stop)
fundamentals  annual FY rows for 5 years (available_from = period_end + 61 days), quarterly income for 6 quarters (+46 days), holdings monthly
membership    all 60 every month except security 13 after delisting (still tracked to label maturity)
```

The world is deterministic (`seed=0`). Extend it only by adding; never change existing values (other tests' expected numbers depend on them).

### 2.2 Recorded Yahoo fixtures (WS02, WS03)

```
tests/fixtures/prices_zfcvindia_2026.csv          raw download 2026-06-01..07-15 ; Stock Splits 6.0 on 2026-06-24 ; Close continuous
tests/fixtures/prices_heromotoco_div_2026.csv     raw download around 2026-07-24 ; Dividends 75.0
tests/fixtures/yahoo_info_samples.json            info dicts for HEROMOTOCO.NS (dividendYield 3.48, returnOnEquity None, debtToEquity 3.57), PNB.NS, one small cap
tests/fixtures/yahoo_statements_heromotoco.json   annual + quarterly statements, earnings_dates
tests/fixtures/nifty500_2026-09-05.csv            the real constituent file (500 rows, 20 sectors)
tests/fixtures/idx_mom30_2026-09-05.csv, idx_qual30_2026-09-05.csv
tests/fixtures/legacy_sample.db                   20-ticker extract of quant_engine.db (all six dates) built by scripts/build_legacy_sample.py
```

## 3. Cross-cutting invariants (property tests; each is one test function)

```
P1  For every as_of and horizon: labels_available(T, h) contains no s with end(s, h) > T                              (embargo)
P2  For every model version: family weights sum to 1.0000 ; each in [0.5/F, 2/F] ; non-active factors weight 0            (I1–I4)
P3  Injecting a synthetic split and dividend into any security's raw series leaves TRI returns unchanged to 1e-6           (T7)
P4  Within every sector group with n >= 5: mean(z) = 0 +- 1e-9, sd(z) in [0.95, 1.05] ; NaN raw -> NaN z                (standardise)
P5  FactorInputs built at as_of raises LookaheadError for any request beyond as_of ; truncating the DB to <= as_of         (T3/T4)
    and recomputing yields identical factor_values
P6  Re-running any command for the same as_of writes no new rows and changes no stored value (idempotency) except          (idempotency)
    evaluations_log entries
P7  The champion's model_weights equal hierarchical EW for every as_of (challenger never leaks into the champion)         (I5)
P8  No evaluations row has method 'xsec_t' ; every cumulative IC row has n_eff and ci90_lo/hi                             (statistics honesty)
```

## 4. Leakage tests on synthetic data (WS07; also run monthly on real data as gate G10 + `verify leakage`)

```
T1  shuffle              permute labels within as_of -> |mean IC| < 2 se_hac for every subject
T2  planted signal       rho 0.10 recovered in [0.07, 0.13] through the full pipeline ; production refuses the leaky factor
T3  PIT truncation       identical factor_values from a DB truncated to <= as_of
T4  as_of boundary       SQL over provenance: zero rows with an input dated after as_of
T5  embargo              unmatured row ignored by the learner
T6  forward shift        available_from - 90 days raises IC of a deliberately leaky factor ; not of clean ones
T7  corporate action     TRI invariant under injected split + dividend ; reconciler flags the un-adjusted copy
T8  holdings lag         this run's capture invisible at as_of
T9  survivorship         evaluated set == universe_membership(s) incl. later-delisted security 13
T10 sector leak          IC of sector dummies vs L_h ~ 0
```

## 5. End-to-end acceptance scenario (`tests/integration/test_e2e_synthetic.py`, WS11)

```
given   the synthetic world with all 48 months ingested, factors registered (launch set + plant_mom + plant_noise as shadows)
when    run monthly for as_of = months 46, 47, 48 (skip_fundamentals; offline)
then    runs: 3 rows status ok, dq passed
        labels: every (s, h) with end <= month 48 matured ; security 13 has delisted_partial rows ; security 5's window over the split has status ok and |r| sane
        evaluations: per-month and cumulative rows for every factor/family/model at h in (1,3,6,12) ; plant_mom cumulative 3M IC in [0.06, 0.14] with n_eff >= 12 ;
                     plant_noise |IC| < 0.03 ; NULL_RANDOM percentile computed
        curves: evidence_curve rows for the champion ; learning_curve_points for IC_SHRUNK_v1 with ew_oos_ic ; IC_SHRUNK weights == EW for k < 12 labelled months and != EW after
        portfolios: PF_EW_HIER_v1_TOP30 has 30 positions each month ; PF_BM_EW excess vs BM_EW ~ 0 ; costs > 0 ; turnover_one_way for TOP30 < DEC10
        knowledge: at month 48 a promote_factor proposal for plant_mom exists with criteria all true ; none for plant_noise
        approve as human -> decision approved, ADR file exists -> apply -> factor_registry status active, effective_from = month 49 ; model_versions has EW_HIER_v1 v2 valid_from month 49 ;
                   scores for months <= 48 unchanged (hash compare)
        report: knowledge/reports/<month48>.md exists ; verdict line present ; no IC printed without n_eff ; the word 'alpha' absent (n < 24 months)
        ui: five data files written ; every IC record carries n_eff and ci ; export refuses a record with a band removed
        ledger: export for each month ; db verify ok after rebuild
and     running month 48 again writes no new rows (idempotency) ; running with the 349% dividend-yield injection exits 2 and the report says blocked
```

## 6. Legacy migration acceptance on the real database (`QUANT_LEGACY_REAL=1`, WS10/WS11)

```
pre     prices backfill --tickers-from legacy has run ; quant_engine.db is the committed file (md5 recorded in PROGRESS)
then    daily_predictions 2,543 rows / active_weights 12 / performance_tracking 4,773 unchanged
        legacy_snapshot_map 6 rows ; 06-12 superseded ; 06-14 -> 2026-06-12 ; 07-11 -> 2026-07-10
        scores track legacy: 1,997 rows per legacy model ; factor_values legacy_*: 1,997 x 11
        ZFCVINDIA L1->L2 label r_arith in [-0.30, +0.30]
        attribution with --legacy-quotes reproduces red_team_review.md within +-0.01:
           final -0.063 / +0.092 / +0.117 ; momentum alone -0.033 / +0.030 / +0.125 ; fundamentals +0.045 / +0.058 / +0.050
        every legacy run has dq_status legacy_defects and is_clean 0 ; no learning_curve_points with track legacy
        second migrate-legacy raises AlreadyMigrated
```

## 7. Runtime and size budget (measured at sign-off, recorded in PROGRESS.md)

```
pytest -q offline                         < 90 s
run monthly (real, incl. fundamentals)    < 60 min   (fundamentals ~30 min at 0.5 s/ticker x 500 x ~7 calls; prices ~3 min; rest < 5 min)
run monthly --skip-fundamentals           < 10 min
prices backfill (10 y, ~550 names)        < 15 min
run backfill-track (128 months x price factors)   < 20 min
quant.db after month 1                    < 60 MB ; growth < 2 MB / month after VACUUM
repo size after month 1                   < 200 MB ; data/prices_daily.sqlite git-ignored (~150 MB local)
UI payloads                               < 1.5 MB total
```

## 8. Sign-off checklist (owner runs `scripts/signoff.sh`; every row must be PASS)

```
#   check                                                             command                                                     pass condition
1   unit + property + leakage + integration tests                     python -m pytest -q                                          all green, < 90 s
2   legacy suite still green after the move                           python -m pytest legacy/tests -q                             58 passed
3   schema complete                                                   python -m quant db init --db /tmp/x.db                       39 tables
4   ledger round trip                                                 python -m quant db verify                                    ok
5   real migration                                                    QUANT_LEGACY_REAL=1 pytest tests/integration/test_migrate_legacy.py -q   green incl. attribution
6   first live run                                                    python -m quant run monthly --as-of <D>                      exit 0, < 60 min, report committed
7   leakage on real data                                              python -m quant verify leakage --as-of <D>                   T1..T10 PASS
8   PIT reproducibility on real data                                  python -m quant verify pit --months 3                        identical
9   report reproducibility                                            python -m quant verify report --as-of <D>                    diff empty
10  invariants                                                        python -m quant model check                                  ok
11  governance integrity                                              python -m quant kb queue ; ADR check                          every decision has an ADR ; budget ledger printed
12  backfill falsifier                                                python -m quant run backfill-track ; read report block         mom_12_1 12M IC cum > 0 with the survivorship caveat printed
13  UI                                                                open ui/index.html                                            8 tabs ; no console errors ; footer sentence ; legacy hollow points
14  repo hygiene                                                      git status ; python -m quant db size                          no *.sqlite staged ; sizes within §7
15  docs                                                              read AGENTS.md, README.md                                     describe V2 ; no unmeasured performance claim
```

## 9. What a reviewer should try to break (adversarial checklist for the owner)

- Change a factor's formula without bumping its version → `factors sync` must refuse / G9 must block next month.
- Add a seventh hypothesis in a year → `kb hypothesis new` exits 3.
- Approve a model promotion as `llm:` → exit 3.
- Delete `data/prices_daily.sqlite` → `prices backfill` rebuilds; `manifest verify` reports no differences.
- Edit `config/quant.toml` `[learning] k_shrink` without a decision → `db init`/`run monthly` refuse.
- Run `run monthly` twice for the same month → second run: no new rows.
- Inject a 40% one-day gap without an action into a copy → `data ca detect` lists it; labels over the window are `excluded_ca` until cleared.
