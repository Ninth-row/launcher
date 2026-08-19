# Wine producer scraper — setup

Monitors configured French/EU natural wine shops for named producers, prices
each hit against reference prices observed from its own crawl, and sends one
digest email per run on anything alert-worthy. Runs every two hours via GitHub
Actions, politely (robots.txt, rate limiting, backoff, a circuit breaker and a
request budget — see `crawler.py`).

## 1. Install dependencies

```
pip install -r requirements.txt
```

## 2. Configure GitHub secrets

In the repo's Settings → Secrets and variables → Actions:

| Secret               | Value                                            |
|-----------------------|--------------------------------------------------|
| `GMAIL_SENDER`         | The Gmail address the alert is sent from         |
| `GMAIL_APP_PASSWORD`   | A Gmail [app password](https://myaccount.google.com/apppasswords) for that address (not the account password) |
| `NOTIFY_EMAIL`         | Where the alert should be sent                   |

Those three are the only external configuration there is. The workflow
(`.github/workflows/scraper.yml`) reads them as environment variables at run
time, sends them to Gmail's SMTP server and nowhere else, and never prints
their values.

There is deliberately **no** contact-email variable. `CONTACT_EMAIL` used to
be documented here and `crawler.py` now ignores it: the User-Agent is sent to
every shop on every request and printed into Actions logs, which are
world-readable on a public repo, so it identifies nobody. It carries
`BOT_NAME` alone. To opt back in to a contact that gives nothing away, set
`CONTACT_URL` to a URL — `Crawler.__init__` raises on anything containing
`@`, and a test asserts the default agent stays bare.

## 3. Add / confirm shops

Shops live in the `SHOPS` list in `scraper.py`. Each entry needs a
`platform` (`shopify`, `woocommerce`, or `html`), the fields that
platform's fetcher needs, and a `verified` flag. `main()` skips any shop
with `verified: False` before making a network call, because that flag means
the entry came from research rather than a real observed response (platform
assumed, selectors invented).

22 shops are verified and fetched on every run, so a normal run does real
work as it stands. Five are unverified and each has a tested reason recorded
in `CLAUDE.md` — 403, a dead domain, a guest price wall, a JS gate, and one
whose `robots.txt` refuses every path. Those five are not a to-do list: four
of them are a shop saying no, and the answer is not going there.

To bring shops online, run the **Probe Shops** workflow with **apply**
ticked. It fetches each shop's real endpoint, and for every shop that
returns a parseable catalogue it saves the real response as that shop's
fixture (trimmed, keeping every producer match), corrects the platform and
sets `verified: true` -- then runs the full test suite and only commits if
it passes. Shops that fail to probe are left unverified with the reason
logged.

Run it read-only first (apply unticked) to see what would happen; that
just uploads a report and the real responses as an artifact.

## 4. Run locally

Normal run (sends a digest email on anything alert-worthy — requires the
three secrets above):

```
GMAIL_SENDER=... GMAIL_APP_PASSWORD=... NOTIFY_EMAIL=... python scraper.py
```

Dry run (no SMTP, no secrets needed — prints the would-be digest to stdout
instead):

```
DRY_RUN=1 python scraper.py
```

Useful crawl-layer env vars (see `crawler.py`):

| Var                     | Effect                                                  |
|--------------------------|----------------------------------------------------------|
| `CONTACT_URL`             | A URL added to the User-Agent. An email address is refused |
| `MAX_REQUESTS_PER_RUN`    | Hard cap on requests this run (default 400, sized to a measured 311-request pass over every catalogue); the run stops cleanly and logs unreached shops when hit |
| `MAX_RUN_SECONDS`         | Wall-clock cap (default 2700). Checked between shops, so it drops whole shops where the request budget degrades one catalogue — the budget must bind first |
| `FRESH=1`                 | Bypass the 6h disk cache for this run                    |
| `FORCE_REPORT=1`          | Report unconditionally, even with nothing new. Set by the workflow on `workflow_dispatch` only |

State/output files (all gitignored, all safe to delete): `seen.json` (per-item
cooldown state), `observations.json` (the observed price pool the references
are drawn from), `.cache/` (the disk cache), `hits.json` (every evaluated hit
from the last run, regardless of whether it was alert-worthy) and
`coverage.json` (the per-shop coverage table). They were not all gitignored:
`coverage.json` was tracked, and the copy in the repo was test output naming
three shops that do not exist (`zzz-shopify`, `zzz-woo`, `zzz-html`), left by
a run predating the fixture that redirects it to a temp dir. Two workflows
`git add -A`, so an untracked state file in the tree is one probe away from
being committed.

Deleting `observations.json` is safe but not free: references are observed
from our own crawl, so a fresh pool classifies more hits as `NOREF` until
enough shops have been read again.

## 5. Check the price book

```
python pricebook.py --stale
```

Lists every producer in `prices.yaml` that's still `verified: false` or
whose `last_verified` is more than 180 days old.

`prices.yaml` is an **optional override**, not the primary source, and
leaving it blank is the normal case. Reference prices are observed from our
own crawl (`market.py`): every priced listing is recorded to
`observations.json`, and a hit is scored against what other shops charge —
same cuvée and vintage first, then the cuvée's other vintages, then the
producer's own line, with `None` at the bottom rather than a guess. A
hand-entered `reference_750_eur` only outranks that when you also set
`verified: true`; an unverified one deliberately ranks *below* observed data,
because a guessed number produces a confident wrong verdict where no number
produces an honest `NOREF`.

Nothing in this project fetches Wine-Searcher — it is blocked and against
their terms. Fill a number in only from your own knowledge of the range, then
set `verified: true` and `last_verified` to today's date.

The one place a human number is genuinely needed is `lines:` — a producer
selling several ranges under one surname (Ganevat's domaine Côtes du Jura at
~€91 against a négoce line at ~€40) cannot be separated by a pooled average,
and the observed pool cannot split what a label does not distinguish.

## 6. Run the tests

```
pytest tests/ -q
```

Every shop in `SHOPS` has a saved fixture response under `tests/fixtures/`
and a test asserting what it should match. Run these before committing any
scraping change.

## 7. The dashboard

`wine.html` is a generated status page and control panel: current shops and
their platforms, producers and reference prices, and buttons to run the
workflows or add config. Regenerate locally with `python dashboard.py`; a
workflow rebuilds it whenever `scraper.py`, `prices.yaml` or `dashboard.py`
changes on `main`.

Never hand-edit `wine.html`, and never commit a rebuilt copy on a feature
branch — `main` rebuilds it on the push after a merge, so a branch-side copy
only produces a merge conflict on a file whose correct resolution is always
"regenerate it". No test reads it from disk; they render through
`dashboard.render()`.

If GitHub Pages is enabled for the repo it is served at
`https://<owner>.github.io/launcher/wine.html`, and the root `index.html`
redirects there. The repo slug the page drives is derived from the git remote
by `dashboard._repo_slug()`, so the generated page is correct under any owner
and needs no edit if the repository moves.

It holds no credentials: the buttons call the GitHub REST API directly from
the browser, and the token that authorises them is entered once per device and
kept in `localStorage` — never written into the generated file. A fine-grained
token with **Only select repositories** → this repo is what it expects.

## 8. Changing config from a phone

Two issue forms, linked from the dashboard. Each one adds, updates **and**
removes -- there is no separate "edit" path to hunt for.

**Producers** -- name, aliases, region, reference price.
- Naming an existing producer *updates* it. Fill only what you want to
  change; blank fields keep their current value. So correcting a price is
  the same one-step action as adding a producer.
- Ticking "checked myself" marks the price verified and stamps today's
  date; you never type a date. It's refused if there's no price to vouch
  for.
- The bulk box adds several at once, one per line:
  `Name | aliases | region | price` (region and price optional).
- Ticking Remove in the danger zone deletes the producer instead.

**Shops** -- short name and URL.
- An existing name re-points that shop and resets it to `verified: false`,
  since the old verification no longer applies. Re-probe it afterwards.
- Ticking Remove deletes the shop and its fixture.

Submitting a form runs `.github/workflows/apply-config.yml`, which
validates the input, edits `scraper.py`/`prices.yaml`, rebuilds the
dashboard, runs the full test suite, and only then commits to `main`. It
comments the commit link on the issue and closes it. There is no pull
request to merge -- if the tests fail nothing is committed, and every
change is revertible through git.

Only issues opened by the repo owner are processed -- the repo is public,
so this gate stops a stranger from driving edits to `scraper.py`.

These forms are also how a config change stays out of the commit log under
your own name. Everything they commit is authored by `github-actions[bot]`,
whereas editing a file through GitHub's web editor attaches your account's
name and email to a public commit — which is what put the previous owner's
login back into this repository's history after it had been migrated out. Use
the forms, or a branch, rather than the web editor.

## Schedule

Every two hours, UTC (`0 */2 * * *`). It used to ask for hourly and GitHub
never delivered it: a public repo's scheduled runs are queued at low priority,
so `0 * * * *` arrived at 20:22, 22:08, 23:55, 02:24, 05:43, 08:27, 11:17 and
13:55 — and twice a run was cancelled without ever being given a runner, which
arrives as a failure email that looks exactly like a broken scraper and is not
one. Asking for 24 runs to receive 10 only adds queue pressure. A
`concurrency` group keeps a delayed run from overlapping the next one.

Two hours also sits inside the crawler's 6h cache TTL, so the full crawl is
paid for roughly four times a day rather than every run.

Each run also has a `workflow_dispatch` trigger for on-demand runs from the
Actions tab, with inputs to bypass the cache (`fresh`), override the request
budget (`max_requests_per_run`), or print the digest instead of sending it
(`dry_run`). A dispatched run sets `FORCE_REPORT=1` and so always emails,
even when nothing is new — that empty table is the only way to tell "nothing
new" from "credentials expired" from a button you pressed.
