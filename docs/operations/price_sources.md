# Closing price sources

Closing prices come from a chain of sources. This page says what the chain is, how to use it and where it stops. The rule is MASTER_SPEC R18.

## Order of sources

1. Yahoo. It gives the history and the corporate-action adjustments. It stays first.
2. The NSE end-of-day file, for members that still have no close.
3. The BSE end-of-day file, for members that are still missing after NSE.

The chain looks at the as-of session and the two sessions before it.

Why this order. NSE is the primary exchange for these stocks and its closes equal Yahoo's. BSE is a second exchange. Its close can differ a little, so it comes last.

A close that is already stored is never overwritten. A file only fills a member that has no close for that date.

## Measured facts (13 Nov 2026)

* Yahoo had 325 of 501 index members a day later. 176 closes were missing.
* The NSE file held 175 of those 176 missing closes. The one left is a placeholder for a company that is not listed yet.
* On a copy of the real data with no close at all for that day, the NSE file alone filled 500 of 501 members.
* NSE equals Yahoo on 324 of 324 names that both have.
* The BSE file covers 498 of 501 members. Its closes differ from NSE's by a median of 0.08% and at most 1.7%.
* Both files for 13 Nov 2026 carried a Last-Modified time of about 16:35 India time.

## Manual mode is the default

The default is `mode = "manual"` in the `[price_sources]` section of `config/quant.toml`. In this mode the engine never makes a request to an exchange. You download the file and put it in the inbox folder.

Why. NSE's terms of use prohibit systematic or automated collection of data from its website. Its copyright policy allows a person to view, print and download content for personal, non-commercial or educational use, with acknowledgement. A person who downloads one file by hand is inside that. A program that fetches it every evening is not. The two pages are https://www.nseindia.in/nse-terms-of-use and https://www.nseindia.in/nse-copyright (read on 14 Nov 2026).

BSE's disclaimer page (https://www.bseindia.com/static/about/disclaimer, read on 14 Nov 2026) prohibits reproducing, redistributing or transmitting its information. It has no clause about automated collection. Its terms page covers downloadable software only.

## The step on a month-end evening

1. Open the exchange's reports page in a browser.
2. Download the day's "CM-UDiFF Common Bhavcopy Final" file. Keep the official name, for example `BhavCopy_NSE_CM_0_0_0_20261113_F_0000.csv.zip`.
3. Put it in the inbox folder: `data/inbox/prices/` (the config key `inbox_subdir`, under the data directory). The unzipped `.csv` is also accepted. The BSE file is `BhavCopy_BSE_CM_0_0_0_20261113_F_0000.CSV`.
4. Run `scripts/q.sh prices update --through 2026-11-13`, or wait for the next scheduled retry.

`prices update` prints one line per missing file when closes are still missing for the `through` date. Each line gives the file name, the official URL and the inbox folder. The URL is printed for a person to open. The engine does not fetch it.

What happens to a file. The engine reads it, parses it, archives the exact bytes, registers a capture and fills the missing closes. The capture time is the time the engine first read the file, not the file's own modification time. The capture's `source_version` is `manual-file:` plus the file name. After a clean parse the file moves to `data/inbox/prices/done/`. If a file of that name is already there, the first 8 characters of the file's sha256 are added before the extension. A file that fails to parse (not a zip, wrong session, no trade date, missing column) moves to `data/inbox/prices/failed/` with the same name-clash rule, and records one `PRICE_FALLBACK_SOURCE_FAILED` event, so the same failure is not recorded on every run. A file the engine cannot read at all (for example a permission error) stays where it is and records the event on each run until you fix it. The file moves only after every write for it has succeeded (price rows, capture, events), so a failure part-way leaves it in the inbox.

A file that is not there is not an error. It writes no event.

## Auto mode

`mode = "auto"` makes the engine use a matching inbox file when there is one and otherwise fetch the file itself. Switching it on is the owner's decision, because of the terms above. Do not switch it on to save a download. The shipped default stays `manual`.

## How to enable

The chain does nothing until a human records the decision in that database:

```
scripts/q.sh kb decide --kind rule_change --subject price_fallback_sources --title "..." --evidence "..." --actor-kind human --by human:NAME
```

An agent must never record this decision. The decision turns the chain on. It does not choose the mode; the config does. Recording it is also the owner's acceptance of the exchanges' terms of use, as far as they are described above.

## Events

* `PRICE_FALLBACK_USED` (INFO): a source filled at least one close. Detail: date, source, kind, capture_id, filled, still_missing.
* `VENDOR_CLOSE_DISAGREEMENT` (WARN): a file's close differs from a stored close by more than the tolerance. The tolerance is `disagreement_warn` (0.5%) for the NSE file, which is the same exchange as Yahoo's closes, and `disagreement_warn_cross_exchange` (2%) for the BSE file, which is another exchange. Measured on 13 Nov 2026: against stored NSE closes 14 of 495 BSE names differed by more than 0.5%, at most 1.7%. Detail: date, source, count, compared, tolerance (the one used) and a sample of up to 10.
* `PRICE_FALLBACK_SOURCE_FAILED` (WARN): a file or a fetch could not be used (bad file, wrong session, unreadable file, timeout or other network error). The chain goes on to the next source. In auto mode, when a fetch gets no HTTP answer at all (a timeout or a refused connection), that source is skipped for the remaining sessions of the same run, so the event is written once. A file that is not published yet (HTTP 404) or not in the inbox is not a failure and writes no event.

## Limits

* The files carry no split or dividend data. A corporate action on a filled day shows up as a price-jump flag for a person to resolve.
* BSE is another exchange. Its close can differ from NSE's.
* REITs and InvITs trade in other series: NSE `RR` and `IV`, BSE `IF`. The series lists include them, after the equity series.
* A fallback row has split ratio 1.0 and dividend 0, because the files carry neither. When the vendor later delivers the same bar with the same close and a dividend or a split ratio, the vendor row is stored as a new observation and becomes the newest visible row.
* When a BSE-filled close differs from the vendor's later close by more than 2%, the vendor row is not stored. It goes to the existing quarantine (`prices_daily_quarantine`) and gate G5 shows it to a person.
* A capture row is kept even if the run's staging is rolled back, because the price rows are already committed in the separate price store. If price rows exist for a capture that has no row (an earlier rollback), the capture is registered again with the earliest `observed_at` of those rows as its time, never the current time.
* Gate G3 and the cutoff rules are unchanged. A close first seen after the cutoff is still late.
* The terms of use of the exchanges' websites are the owner's to accept. This page reports what they say; it is not legal advice.
