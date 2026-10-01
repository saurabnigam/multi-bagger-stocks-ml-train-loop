# Triage evidence sources

What `triage collect` may fetch, what it stores, and what a source that did not answer is allowed to mean.
Code: `quant/triage/sources.py` (envelopes, entity match, feeds) and `quant/data/yahoo.py`
(`YahooClient.actions`, `YahooClient.news`). Design: evidence triage design, sections 3, 4 and 8.

## Rule for every source

A source returns one of four statuses. Only `ok` carries evidence and only `ok` can support a rule.

| Status | Meaning | May it support a rule? | What it does not mean |
|---|---|---|---|
| `ok` | The source answered and there is at least one envelope to cite. | Yes | That the evidence is decisive. |
| `empty` | The source answered and held nothing usable. | No | That no such action or headline exists. yfinance also returns an empty frame for a ticker it could not read. |
| `error` | No answer, or an answer in a shape the code does not recognise (missing or renamed field, bad XML, HTTP failure). | No | That there is no news. |
| `skipped` | Not asked: no Yahoo ticker, or no feed enabled. | No | That the source was checked. |

`SourceResult` refuses to be built as `ok` without an envelope, or as any other status with one.

`supports_rules` says the source gave an answer, not that the answer is relevant. A news result can be `ok`
when every headline is context only (none names the entity); rules and the validator must count only envelopes
with `entity_match` true. For Yahoo actions, if either the split or the dividend column is missing or renamed,
the whole result is `error`: a table that cannot be read is never recorded as zero splits.
`sources.source_status_map(results)` gives the pack's `source_status` block (status, short detail, envelope
count per source). A data-shape problem never raises into the caller: the result carries the status.

## Sources

| Source | In v1 | Stored | Notes |
|---|---|---|---|
| Yahoo corporate actions | Yes, through `YahooClient.actions` (same throttle, same 429 retry as `bundle`, at least 0.5 s before every attempt) | One undated envelope per collect: splits within 400 days of the ex-date, dividends within 5 days | Behaviour for `.NS` tickers is unverified. |
| Yahoo news | Yes, through `YahooClient.news` (same path) | One envelope per headline, at most 10: headline (at most 200 characters), link, provider timestamp | No summary, body or publisher name is copied, at any layer. |
| RSS feed (SEBI) | No: `sources.FEEDS` ships empty | One envelope per item that names the entity | See "Enabling a feed". |
| IRDAI, RBI, NSE, BSE, company IR | No | Human `triage attach` only | `BLOCKED_HOSTS` refuses IRDAI, NSE and BSE hosts in code, including across redirects. |
| Paid feeds | No | n/a | Owner decision. |

## Yahoo actions envelope

Provenance `yahoo_actions`, `source_class` `other`, `relevance` `context`, `entity_match` null, `published_at`
null. Yahoo's action table has an effective date, not a publication time, and `published_at` is never
defaulted, so this envelope is context for the engine's vendor-conflict features and can never be the
decisive citation of a proposal. It holds no fetch time, so a second collect with the same answer registers
the same capture (`evidence.register` reuses it).

Consequence for the rules and the validator (open, owned outside this package): validator rule 3 requires a
non-null `published_at` on every citation behind a non-unknown label, so this envelope can never be cited.
Rule R4 (VENDOR_SPLIT) must therefore read the vendor split from the pack's features or from this envelope's
`typed_fields` directly, and the validator must not demand a citation for it. The R4 path needs a test that
runs with only an undated `yahoo_actions` envelope.

`typed_fields` (at most 15 of the 16 the envelope allows):

| Key | Meaning |
|---|---|
| `ticker` | The Yahoo ticker that was queried. Not verified to belong to the ISIN. |
| `window_days` | 400. Splits within plus or minus this many days of the ex-date are counted. |
| `n_splits` | Number of splits in that window. 0 is a recorded answer from an `ok` response. |
| `split_<k>_date`, `split_<k>_ratio` | k = 1 to 3, nearest to the ex-date first (ties: earlier date). Ratio as the vendor gives it: 2.0 means two new shares per old, the same convention as the store's `split_ratio`. |
| `splits_truncated` | True when more than 3 splits were in the window. |
| `n_dividends_pm5d` | Dividends within 5 days of the ex-date. |
| `dividend_<k>_date`, `dividend_<k>_amount` | k = 1 to 2, nearest first. |

The window is wide because, in the recorded cases, a vendor split sits 64 to 277 days from the jump it
explains (CGCL, GPIL, MOTILALOFS, TRENT). Rule R4 reads splits in that whole 400-day window and applies its own tests: the ratio is within 3% of 1/gross (downward jump) or of gross (upward jump), the split is more than one session from the jump, and the price store shows no cancelling jump on the split date. Rule R3's demerger check keeps its own five-day window.

## Yahoo news envelope

Provenance `yahoo_news`, `source_class` `news` (never a primary class), `published_at` from the provider's
timestamp with basis `source` and precision `timestamp`. A missing or ambiguous timestamp (none, or no
timezone) stays null. A headline dated after the moment of the fetch is dropped (`published_after_now`).
`typed_fields` is `{ticker}`. `excerpt` and `body_sha256` are null.

Both response shapes yfinance has used are read: the nested `content` object (`title`, `pubDate`,
`canonicalUrl`) and the flat one (`title`, `link`, `providerPublishTime`). An item without a readable
headline is counted in `RawNews.n_unparsed`. A non-empty response where no item has a headline is `error`,
not `empty`.

### Entity match

`sources.match_entity(text, symbol=, names=)` compares whole tokens, never substrings. In order:

1. A full company name (legal suffixes such as Limited and Private removed) as consecutive tokens, case-insensitive. Basis `name_tokens`.
2. The exchange symbol written in upper case as the symbol. Basis `symbol_token`. The word "idea" is not IDEA.
3. A company name that is one word of at least 3 characters. Basis `name_single_token`.

`Trentham` is not `Trent`, `Exchanger` is not `Exchange`, `Lloyds` is not `Lloyd`, and `Tata Steel` is not
`Tata Motors`. The result sets `entity_match`, `relevance` (`entity` or `context`) and `relevance_basis`.

A multi-word name can be the start of a longer, different company (`Tata Motors` in `Tata Motors Finance`).
When every occurrence of the name is followed, with only whitespace between, by a capitalised word that is not a
legal suffix and not a quarter or fiscal-year label (`Q2`, `H1`, `FY27`), the name is not taken as a match. The
basis is `name_prefix_ambiguous`, `entity_match` is false and the headline is context. The symbol and
single-word checks still run afterwards. One clean occurrence (a lower-case word, a number, punctuation, a
suffix, a period label or the end of the text after the name) is enough for `name_tokens`.

A headline that does not name the company is still archived, as context with `entity_match` false. It cannot
support a claim about this security. It is kept because its keywords (for example a demerger) must reach the
engine's negative check, which then fails safe.

Limits, stated plainly:

- A single-word name can collide with another entity (a person named Trent, PNB against PNB Housing Finance). The basis `name_single_token` says so, and news is graded as news (level N) anyway.
- `name_prefix_ambiguous` trades recall for safety. A title-case headline with a capitalised word straight after the real name ("Tata Motors Reports Record Sales") is graded as context, not as the entity. A longer company name that starts with a different capitalised word the code does not know is caught; a longer name that starts with a lower-case or numeric word is not (for example `Tata Motors 2-wheeler unit`). News stays at level N either way.
- A company renamed after the headline was written (or before) can miss. Callers should pass every known name, current and former, in `names`.
- Matching is on the headline only. There is no body.

## RSS feeds

`FeedSpec(name, url, source_class)`. The name must be `sebi_rss`: every feed envelope is labelled with the one
feed value in the closed provenance set, so `load_feeds` refuses any other name as an error entry before any
fetch. The URL must be https; hosts in `BLOCKED_HOSTS` are refused before any
fetch; `source_class` must be one of the closed set. The parser is RSS 2.0 only, from the standard library,
and refuses any document that is not UTF-8 (a NUL byte or an invalid sequence; a byte order mark is allowed), has a
DTD or entity declaration, is more than 2 MB, or has no `rss/channel`. The UTF-8 rule exists so the DTD check sees
every byte: a UTF-16 document would hide `<!DOCTYPE` from it.

Item `pubDate` handling: a date with no time is a `date`-precision value (the channel's own time is not
borrowed); a time needs an explicit timezone; anything else is null. Only items whose title names the entity
become envelopes (provenance `sebi_rss`, `relevance` `entity`, `entity_match` true, `typed_fields`
`{feed}`); the rest are counted in the result's detail ("N of M items name the entity").

Call pattern for a collect run: `sources.load_feeds()` once, then `sources.feed_results(snapshot, isin=...,
ex_date=..., symbol=..., names=...)` per anomaly. `sources.collect_feeds(...)` does both for one anomaly.

### SEBI feed: what was checked

These facts are from the design's verification session, not re-checked by the implementer (no network call
was made in this work package).

| Item | Finding |
|---|---|
| URL | `https://www.sebi.gov.in/sebirss.xml` |
| Format | RSS 2.0, about 15.9 KB, 30 items spanning 28 to 30 Sep 2026, `ttl` 60 |
| Item date | Date only (`30 Sep, 2026 +0530`). The channel `pubDate` has a time. |
| robots.txt | Allows everything except asset paths |
| Terms of use | Not found on the feed page. Verdict: not recorded. |
| Window | About 3 days. An item vanishes unless the feed is captured daily. |
| Coverage | SEBI orders, circulars and press releases. Not IRDAI, RBI or exchange events. |

### Enabling a feed

1. Read the site's terms yourself and add a row to the table above with the URL, the date and a verdict.
2. Edit `FEEDS` in `quant/triage/sources.py`, for example `FEEDS = (FeedSpec("sebi_rss", "https://www.sebi.gov.in/sebirss.xml", "regulator"),)`.
3. Decide how it is captured daily (a cron running `triage collect`), because of the 3-day window.

The design's recommendation is to leave the SEBI feed off: it does not cover the known cases. A feed
other than `sebi_rss` needs a new value in the closed provenance set in `quant/triage/types.py` and a change
to `_validate_feed` and `feed_results`, which are coded to the one value.

## Point in time

`captured_at` comes from the run clock in `evidence.register`; nothing in `sources.py` carries or chooses a
capture time. The `clock` argument of `news_result` is used only to drop a headline dated after the fetch.
`pit.admissible` makes the final decision at build time.

## Not verified

- What Yahoo returns from `actions` and `news` for `.NS` tickers today (field names, whether news is ever non-empty). The code tolerates both known shapes and reports `error` or `empty`; it does not assume either is healthy.
- NSE and BSE terms of use, IRDAI or RBI feeds, paid feed prices.
- The live test (`QUANT_NETWORK=1 python -m pytest -q -m network tests/unit/test_triage_sources.py`) asserts shapes only and was not run in this work package.
