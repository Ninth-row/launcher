# Wine producer scraper

## Autonomy

Agents may not ask the user anything. If an agent would stop to ask, it
invokes the `decision-proxy` agent instead and acts on its verdict (see
`.claude/agents/decision-proxy.md` and `decisions/standing-decisions.md`).
On ESCALATE, the caller continues with all remaining unblocked work — an
escalation is logged to `decisions/open-questions.md` for the human, it is
never a reason to idle waiting for one.

## Architecture

No framework, no database, no queue — eight plain files, each owning one
concern, wired together by `scraper.py`:

1. **`crawler.py`** — the only module that calls `requests`. Every fetch
   goes through `Crawler.get()`: robots.txt (cached per host, `Crawl-delay`
   honoured), a minimum 3s + 0-2s jitter per-host rate limit, concurrency 1
   per host, exponential backoff on 429/503 (or `Retry-After` if present),
   a circuit breaker (3 consecutive failed requests skips that host for
   the rest of the run), conditional requests (ETag/Last-Modified, a 304
   reuses the cached body), a 6h disk cache (bypass with `FRESH=1`), and a
   hard `MAX_REQUESTS_PER_RUN` budget (default 400, sized to a measured
   311-request complete pass over every catalogue, and to fit
   `MAX_RUN_SECONDS` at a pessimistic 5.5s per request) that stops the run
   cleanly and logs which shops weren't reached. It also refuses to believe
   a 200 that is a bot challenge: `looks_like_challenge` reads a meta-refresh
   to a captcha path, or one of a handful of interstitial phrases on a page
   that links nowhere, and raises `Challenged` **before** the cache write,
   without retrying.
2. **`scraper.py`** — loops over `SHOPS`, dispatching each to a platform
   fetcher (`fetch_shopify`, `fetch_woocommerce`, `fetch_html`, all taking
   a `Crawler` instance) that returns `{text, title, price, url,
   variant_title}` items, then runs `match_producers(text)` (alias matching
   against `PRODUCERS` through `textnorm.match_key`, so accents, case and
   separators are all out of the comparison) to produce raw hits. A match on
   a listing that is out of stock is not dropped but set aside on
   `ShopResult.sold_out`, which is what lets the run distinguish a broken
   alias from a producer that is simply sold out everywhere.
3. **`autoselect.py`** — reads a catalogue nobody wrote selectors for.
   Most HTML shops match none of the generic `div.product` guesses because
   their markup is their own (leszinzinsduvin is hand-rolled PHP serving
   `/vin-<id>-<region>_..._<producer>.html`), and nine bespoke selector
   sets is nine things to maintain that each break silently on a restyle.
   Instead the structure is derived: find the innermost elements whose text
   holds a currency-adjacent price and no link (a card is not a price cell),
   climb to the widest ancestor still describing one product *and* holding a
   product link, group those by shared parent, and take the busiest group.
   Innermost-and-full-text, not own-text: WooCommerce and Squarespace both
   render `<span>12.50<span>€</span></span>`, so an own-text rule sees no
   price at all and the whole grid is invisible -- which is exactly what hid
   vinovivo's 315 wines and purovino's 122.
   `fetch_html` uses configured selectors when they match and falls back to
   this when they don't, walking pages via `find_next_page` (`rel="next"`,
   then "next"/"suivant" labels, then a `next`/`suivant` class token for the
   pager arrow that carries no text at all). Returns `[]` rather than
   guessing when a page has no repeated priced structure.
   An optional `catalog_path` on a SHOPS entry points at the catalogue when
   the landing page isn't it, and `catalog_paths` (a list) exists because
   winenot.fr and vinnouveau.fr keep their wines under region categories
   (`/12-alsace`, `/19-jura`, `/21-loire`) with no "all wines" page at all --
   one path there reads one region, which is how winenot came to be
   configured to read its sparkling-wine filter and nothing else. Every entry
   is walked to the end of *itself* -- the page budget is per catalogue, not
   shared, because sharing it spent every page on the first category -- and
   products are deduplicated by URL across them. Look for the union before
   configuring a list: winenot was read as six colour filters for months
   while its own menu linked all six at once
   (`/s/35/blanc-rouge-rose-vin-effervescent-vin-moelleux-vin-mute`, 1236
   wines over 103 pages). Six walks read 1229 over 105 -- seven wines carry
   two colours and the dedupe dropped them -- and a total we compute by
   summing six categories cannot be printed as a coverage fraction, so that
   row stated no total at all. One catalogue states its own size.
   How far to walk is derived
   from the size each catalogue states on its own page one
   (`catalogue_size`: "Affichage 1-24 de 2827 article(s)", "1-10 de 315
   resultats"), which costs nothing because that page is already in hand.
   `MAX_PAGES_PER_SHOP` is only a net against a pager that generates links
   for ever. A pager that links *next* and nothing else states no size: three
   distinct page numbers are required before the highest one is read as the
   last, or a single `?page=2` stops the walk at two pages and calls it
   complete.
   `find_catalogue_links` derives all of this from the shop's own navigation,
   because no list of guessed paths knows what a shop calls its catalogue.
4. **`market.py`** — where reference prices come from. One typed number
   per producer cannot describe a producer selling a negoce cuvee at EUR 30
   and a domaine vin jaune at EUR 250, and the typing grows with producers
   x cuvees x vintages. So the reference is *observed*: every priced
   listing is recorded to `observations.json`, and a hit is scored against
   what other shops charge. A ladder from strongest evidence down --
   same cuvee + same vintage elsewhere, same cuvee other vintages, then
   the producer's own line -- with the confidence dropping at each rung and
   `None` at the bottom rather than a guess. Three things separate
   themselves with no per-producer config: negoce vs domaine (the label
   carries extra names, so `segment()` splits them), vintage (part of the
   key), and cru (already in the cuvee text). Magnums and clavelins are
   normalised to a per-750 equivalent; coffrets are only ever compared to
   other coffrets. Nothing here contacts a price aggregator -- the data is
   the crawl we already do.
5. **`prices.yaml`** / **`pricebook.py`** — the optional manual override (per-producer `reference_750_eur`, optional per-cuvee
   overrides, a `verified`/`last_verified` pair per producer) plus a CLI
   (`python pricebook.py --stale`) listing entries that are unverified or
   older than 180 days. No longer the primary source: a `verified: true`
   figure still wins over observed data, but leaving it blank is now the
   normal case. Nothing in this codebase fetches Wine-Searcher — that's
   blocked and against their terms.
6. **`evaluate.py`** — turns a raw hit into a priced one: parses bottle
   size from the title/variant (including the Jura 620ml clavelin, and
   defaulting to 750ml at low confidence if nothing matches), flags
   coffrets/cases as bundles whose per-bottle price is unknowable, detects
   Burgundy cru tier from the cuvee text for `region: burgundy` producers, computes `expected = reference × tier
   multiplier × format multiplier`, and classifies `DEAL`/`FAIR`/`HIGH`/
   `NOREF`. Never drops a hit — an unverified reference or low size/tier
   confidence sets `caveat: true`, it doesn't suppress anything.
7. **`notify.py`** — one digest email per run, never one per hit. A row
   names the alias that matched it (`Ganevat [ganevat]`), because a
   misattributed producer is otherwise indistinguishable from a correct
   one -- three estates were reported wrongly before this existed. Any
   digest that is sent also carries the run's `notes`: shops that returned
   nothing, and watched producers found nowhere. State in
   `seen.json` (keyed by `sha256(shop + product_url + variant)`) drives a
   30-day per-item cooldown. A hit alerts if it's new, or its price dropped
   >10% since the last alert — a drop is news, so it ignores the cooldown —
   or its classification improved to `DEAL`, which does wait for the
   cooldown because classification is derived from the market pool and can
   flap hourly. A run where nothing qualifies sends nothing and exits 0 —
   that's a valid, successful run. But a whole *week* of them looks exactly
   like broken credentials from the inbox, so after `RECAP_DAYS` with no
   email at all the run sends one recap of everything currently matched
   (`_meta.last_recap_at` in `seen.json`, reset by any email). A run a
   *human* started reports unconditionally -- the workflow sets
   `FORCE_REPORT=1` on `workflow_dispatch` only -- because silence from a
   run you pressed a button for reads as a dead scraper, and twice did. The full
   evaluated hit set always goes to `hits.json` (uploaded as the workflow
   artifact) even when the email itself is empty or capped at 40 rows.

8. **`textnorm.py`** — how text is compared, in one place, as two
   functions that must not be confused. `strip_accents` (NFKD, drop
   combining marks, lowercase) is what `scraper`, `market`, `evaluate`,
   `apply_issue` and `autoselect` each used to carry as a private copy —
   five identical copies waiting for one divergent edit, which would have
   made a producer added through the issue form derive an alias matching
   nothing. `match_key` adds separator folding (`&` → `et`, every other
   non-alphanumeric → space) and exists only for deciding whether a *name*
   matches, so one alias `bruyere houillon` covers "Bruyère-Houillon" and
   "Renaud Bruyère–Houillon" with no per-producer variant. It must never
   touch text a price or vintage regex will read afterwards: it removes the
   currency markers `PRICE_PATTERN` and `market.VINTAGE_RE` use to tell a
   price from a year.

**`discover.py`** answers the three questions that decide whether a shop is
worth adding, because ten suggested from search snippets failed on questions
nobody had asked: is it a shop or a restaurant with a wine list (a menu has
the prices and no product links, which is exactly what `find_products`
requires, so the existing parser *is* the classifier), does it ship to
Denmark (`None` is a real answer -- a page that never mentions shipping is
not a page that refuses), and does it stock a producer we watch (read from
the listings, never the page text: a blog post about Overnoy is not a bottle
of Overnoy). Candidates come from `--url`, from `--from-page` (an importer's
stockist list is the richest source, since these growers are allocated
through one importer per country), or from a search API whose key lives in
the environment and is redacted from anything printed. It changes no config:
it emits `SHOPS` entries with `verified: false`, and `probe.py --apply`
remains the only thing that verifies a shop.

It is deliberately not an Instagram scraper. Hashtag pages are behind a login
wall, the Graph API's hashtag search returns only the last 24 hours and does
not name the account that posted, and automated collection is against Meta's
terms -- the same reason nothing here fetches Wine-Searcher. A test asserts no
string the code uses names the host.

Two more files exist for operating it rather than scraping:
**`probe.py`** detects each shop's real platform from a runner (the dev
sandbox has no egress); with `--apply` it also saves the real response as
that shop's fixture, corrects the platform and sets `verified: true` --
allowed only because the fetch, parse and flag happen in one run against a
real response. **`dashboard.py`** generates `wine.html`, a status page
that also operates the scraper: its buttons call the GitHub REST API
directly from the browser (`POST .../workflows/{file}/dispatches` to run
the full pass, the hot pass or the probe, polling `.../actions/runs` to show
progress, `POST .../issues` to apply a config change), so nothing on it is a
link out to the repo. Every workflow a human is expected to start needs a
button here: `hot.yml` shipped without one, which left the only way to run
the thing built for speed buried in the Actions tab. The token that authorises this lives in the browser's
`localStorage`, entered once per device -- never in the file.
`wine.html` is generated, never hand-edited -- change `dashboard.py`
instead.

Runs every two hours from `.github/workflows/scraper.yml`, which runs the fixture
tests first, best-effort persists state across runs via **two** `actions/cache`
entries -- `wine-scraper-state-` for `seen.json`, `observations.json` and
`watchlist.json`, `wine-scraper-httpcache-` for `.cache` -- and uploads
`hits.json` plus `observations.json` as artifacts. The split is load-bearing
and is documented in its own rule below: under one key the 6.5 MB of fetched
pages evict the few hundred KB that decide what has already been alerted.

Any digest or recap also carries what the run could not do quietly:
shops that returned nothing, producers **matched but sold out everywhere**
(with the shops that had them, so there is somewhere to look again),
producers **found nowhere at all** — an alias signal, now that sold-out
matches no longer land here — and **alias near-misses**, words on a shop's
pages one edit away from an alias that matched nothing.

Every `SHOPS` entry also carries a `verified` flag. `main()` skips any shop
with `"verified": False` before it ever makes a network call — this is for
shops added from research/guesswork rather than a real observed response
(platform assumed, selectors invented). It is set only by `probe.py
--apply`, which fetches, parses and flags in a single run against a real
response; never by hand.

18 shops are currently verified and fetched on every run -- oenovinia was
added through the issue form as #27 and verified by probe run
41322696399. Two shops since
removed, vinovivo and purovino, spent months listed here as "prices are
rendered client-side" — and that verdict was wrong twice over. The price *was* in
their HTML; `autoselect._price_nodes` could not see it, because it accepted a
price only when one element's own text held both the number and the currency
marker, and both shops (like most WooCommerce and Squarespace themes) wrap
the marker in its own tag. The evidence for the old verdict was also drawn
from the wrong page: "vinovivo's index has 330 product links and 6 prices" is
true of its *home page*, while `/shop` carries ten priced cards and says
`1–10 de 315 resultats`. Read a shop's catalogue before concluding anything
about a shop.

**purewijnen** is the shop that taught this list its last lesson. It is
Drupal 7 (`li.leaf`, `/sites/purewijnen/files/`) with no commerce module, and
there is no price anywhere in its HTML: `/nl/wijnen-bestellen`, `/nl/wijnkaart`
and the grower bio at `probe_pages/capture.nl-renaud-bruyere-houillon.html`
hold **zero currency markers** between them. It was written off for that, and
the whole range was a 41-page PDF linked from `/nl/wijnkaart` all along — one
extra request for 800+ wines. `_fetch_via_pdf_list` reads it, and it is
verified. "No price in the HTML" is not "no price": look for the document
before concluding a shop has nothing to read. (Its producer index is real too,
and `find_producer_links` finds our Renaud Bruyère-Houillon in it while
ignoring the Overnoy-Crinquand two lines up --
`tests/fixtures/purewijnen-growers-excerpt.html` keeps that real markup as a
namesake test -- but the link leads to prose.)

Four remain unverified, and all four now have a tested reason:
  - **weinfurore** was added through the issue form on 2026-11-01 (issue #25)
    and probed the same day: **HTTP 403, three times, then the circuit
    breaker** (run 41308647379). That is a refusal, and it is answered the way
    every refusal here is -- by not going there. Nothing tries to get past it,
    and in particular the User-Agent is not dressed up as a browser to see
    whether that helps. The loss is real and worth recording so nobody has to
    rediscover it: the shop is Shopware, prices sit in its HTML with
    schema.org `price`/`priceCurrency` metadata, and it carries a deep Ganevat
    range -- a grower page at `/domaine-ganevat-jura-france` listing both the
    domaine and the Anne-et-Jean-Francois negoce line, Chateau-Chalon 2016,
    and a Vin Jaune 2012 at **0,62 l**, the clavelin the band logic exists
    for. Its platform and selectors are still the issue form's placeholders,
    because only a real response may correct them. As naturavin proved, a
    documented 403 is a fact with a date on it rather than a permanent
    property, so this is worth re-probing occasionally -- but it is not worth
    working around;
  - **feralart** is Wix, and "responded and parsed, but zero products found"
    was wrong about it in the way vinovivo and demainlesvins were wrong.
    `probe_pages/feralart.www-feralartetvin-com.shop.html` holds 40 currency
    markers and 25 `/product-page/` links, and a JSON island stating **12
    products with numeric prices, EUR, `isInStock` and a slug each** -- beside
    `"totalCount":923`. Wix renders its grid in the browser from that island,
    so there is no repeated priced structure for `autoselect` to climb.
    `_fetch_via_wix_island` reads it and walks it: twelve records is **1.3% of
    923**, none of the twelve is a producer we watch, and a reader that
    stopped there would have printed a clean coverage row over a shop it had
    barely opened. How far to walk comes from the total the island states --
    77 pages at twelve a page, confirmed against the shop's own
    `/shop?page=2`, which serves twelve different wines and titles itself
    "2/77". That `totalCount` sits beside an `allProductsCategoryId`, which
    settles what looked like a `catalog_paths` question: `/shop` **is** the
    union, and `/things`, `/champagne` and `/auction-wines` are its category
    filters, so configuring them would read slices of a whole already in hand.
    It stocks Tom Gauditiabois. It is still unverified only because `probe.py
    --apply` is the one thing that may verify a shop -- run it;
  - **cuvee3000** answers every path — `/`, `/en`, `/en/wines`, `/fr`, and
    `/vins.php`, which cannot exist — with the same ~1.3KB of title,
    `<noscript>` and one `<script>`: no anchor, no meta-refresh, and a body
    regenerated per request (1223–1463 bytes across eight paths,
    uncorrelated with path length). A framework redirect publishes a way out
    for every client; this publishes none, so the only exit is executing the
    script. That is a gate, and it is answered by not going there. As of the
    probe on 2026-11-01 it no longer even serves that: all ten candidate URLs
    came back **HTTP 307 with no Location**, which is a redirect naming
    nowhere to go. A different no, and answered the same way;
  - **mifuguemiraisin** is closed, and not for a technical reason: its shop is
    Hiboutik (a French point-of-sale SaaS) at `mifuguemiraisin.hiboutik.com`,
    and **every one of the ten candidate URLs came back `robots.txt
    disallows`**. That is the platform saying no, and it is answered the same
    way a bot challenge is — by not going there. Its own domain is a shopfront
    with nothing to read, which is why the entry points at Hiboutik at all.

**vinnaturelbe** was removed on request, and its finding is worth keeping:
it is PrestaShop 1.6 whose product miniatures carry a name, a stock line and
a "Détails" button and no price at all. Its catalogue was
`/fr/categorie/11-acheter-en-ligne`, and
`probe_pages/capture.vin-naturel-be.fr-categorie-11-acheter-en-ligne.html`
has 40 product links and **zero currency markers** -- catalogue mode, or a
price wall for guests, not client-side rendering. Its `/fr/vignerons` index
listed 41 growers and not one producer we watch, and it had no PDF either,
which is the question purewijnen made worth asking. It was listed above as
"remains unverified" for a while after it had already left the config, which
is how a list of reasons becomes a list of claims.

**demainlesvins** was removed on request after it began answering every
request with HTTP 403 -- three attempts, three refusals, then the circuit
breaker, confirmed by a probe on 2026-09-06. That is a shop saying no, and it
is answered the same way a bot challenge is: by not going there. Nothing here
tries to get past it. It is written up in the past tense because the code it
taught is still live and general, and `_price_from_detail_pages` fires for any
HTML shop whose listings carry no price, not for this one.

It was the third shop written off from the wrong page, and the
one that showed a price can be *present and unreadable*. It was PrestaShop 1.7
running private sales, and its cards genuinely refused a guest a price: 33 of
34 carry "Vous devez être connecté pour voir le prix" where the figure would
be. The old verdict — "1.34MB, 2 prices, 0 product links, 32× connexion" —
came from its **home page**; its categories hold 300 listings, and the price
is public one click away, stated twice on the product page (an Open Graph
meta and PrestaShop's `data-product` island, `show_price: "1"`, with a
`quantity`).

Every scan missed it for one reason worth remembering: the euro is
JSON-escaped inside an HTML attribute (`"37,50\u00a0\u20ac"`), so the raw
markup holds **zero** currency markers and `PRICE_PATTERN` reads no prices on
a page that plainly has one. Parse the attribute and it unescapes for free.
So the catalogue is read with configured selectors — `autoselect` finds a card
*by* its price and cannot read a priceless one, which is the documented
exception that earns them — and `_price_from_detail_pages` opens a product
page only for a listing naming a producer we watch: 4 of 432 captured titles
matched, so the whole catalogue costs about ten requests, capped at 25. Its
sold-out marker is `product-oos` as a CSS class and nothing in the card's
text, which is the biarritz stock column in a new costume.

Two of its captures are kept deliberately, against the rule below that a
shop's captures leave with it: `tests/test_pricewall.py` reads them, and what
it pins is the general price-wall reader rather than this shop. Real markup of
a card that hides its price and a product page that states it twice is not
reproducible once deleted, and the next PrestaShop private-sales shop will
need exactly that test. The rest went with the entry.

**wineshopbiarritz** is the second document shop, and the one that showed the
adapter was reading a document without reading its *columns*. Its site is Wix
with zero prices in 548KB of markup; its catalogue is an 84-page PDF of 1546
rows, and it holds five watched producers — Ganevat, Jules Brochet,
Overnoy/Houillon, Richard Leroy, Tom Gauditiabois — for 12 requests. But it
is an inventory export, not a wine list (`Nb | Nom | Famille | Sous Famille |
Stock | Prix TTC Vente`), and read as purewijnen's list is read it parsed
cleanly and said something false: 1546 wines, none sold out, when the shop's
own Stock column reads 0 against **707** of them, every Richard Leroy and the
Pierre Overnoy among them. `pdflist` now reads that column, the running header
and the "Page n sur 84" footer; `tests/fixtures/wineshopbiarritz-list-excerpt.txt`
keeps the real rows. Read a shop's columns, not just its rows.

`crawler.Challenged` exists because a shop can answer HTTP 200 with a bot
challenge rather than its catalogue: **vinnaturel.fr** serves "One moment,
please... Please wait while your request is being verified", and vinopura.nl
served 221 bytes of meta-refresh to `/.well-known/sgcaptcha/` before it was
removed from the list. Both read as healthy for weeks — one as "ok, 0
products", the other as a "parse error" — and both went into the 6h cache, so
each challenge stayed true for six more runs. Such a shop now gets a `blocked`
coverage row and a line in the digest. Do not try to get past a challenge: it
is a shop saying no.

Shops leave the list too. vinopura, volatilewines, purovino, biowijnclub,
vinifine, lespeauxdevins, vinibee, vinovivo, puurwijnshop, petitescaves,
vinnaturelbe, demainlesvins, naturavin and vinscheznous were removed on
request.
Between them the first five were 726 products and no hits, and purovino had
only just been made readable; the next three were 1320 products and two hits
(vinibee's Richard Leroy) at 13 pages a run; puurwijnshop was 709 listings and
never a hit, and demainlesvins had started refusing us with 403.
**naturavin** is the one that left after *succeeding*: it is written up above
as "blocks us with 403", and on 2026-11-01 it stopped doing that -- probe run
41286960093 read it via the WooCommerce Store API, 87 products, and the
probe verified it and corrected its platform from `html`. It was removed the
same morning because those 87 products matched **none** of the 20 producers
watched. The cost was small (87 products is one page at `WOO_PAGE_SIZE`, so
about two requests) but a shop that stocks nothing we hunt is a shop whose
only contribution is a row in the coverage table. It also showed that a
documented 403 is a fact with a date on it, not a permanent property.
**vinscheznous** simply stopped existing: `www.vinscheznous.com` has failed
DNS resolution on every probe since, most recently run 41286960093 on
2026-11-01 (`Failed to resolve ... Name or service not known`). A host that
does not resolve cannot start answering again by itself, so there was nothing
left to wait for. Removing a
shop is config, not archaeology -- the entry, its fixture and its captures go
together, and git remembers the rest. The one exception is a capture a test
still reads for a *general* behaviour rather than for that shop: two
demainlesvins pages stayed for `tests/test_pricewall.py`, because real markup
of a price wall cannot be re-fetched from a shop that has closed the door.

Secrets (`GMAIL_SENDER`, `GMAIL_APP_PASSWORD`, `NOTIFY_EMAIL`) are the
only external configuration; everything else is in this repo. Those three
reach Gmail's SMTP server and nothing else -- they are never put in an
HTTP header or printed.

## Rules

- Do not invoke a subagent for a one-line edit. Just make it.
- Adding a producer name to `PRODUCERS` is a config change, not an agent
  task — edit the dict directly.
- Every scraping change (new shop, adapter fix, selector change) must pass
  the fixture tests in `tests/` before commit.
- A price filter is not a listing. PrestaShop's faceted slider renders as
  "Prix 22 € 550 €" linking `/prix`, and on a caves-carriere grower page read
  at `min_blocks=1` it was the *only* group -- so all eight rows that shop put
  in a digest were the widget: a dead link, the grower's name attached by the
  producer-index reader, and the slider's minimum read as the price, which
  against a Ganevat reference is a permanent DEAL. `autoselect.FILTER_LABELS`
  judges a block by what is left once the prices are removed. Requiring a
  label word is the whole of it: "prices and nothing else" also describes a
  card that is an image and a price with the wine named only in its href,
  which is how vinovivo's grid read, and a test keeps that markup.
- A one-word title is a badge, not a wine. lacaveduchateau puts the region
  inside the product link, so `_title_for` named seventeen rows a run
  "Corse", "Loire" or "Bourgogne" while the img alt read "Clos Canarelli,
  Tara Di Sognu 2024 Rouge 75cl". A candidate of one word is taken only when
  nothing longer is offered -- a bottle is at least a grower and a cuvee.
- The buy button is a stock statement, and on some shops the only true one.
  winenot's PrestaShop theme stamps `schema.org/InStock` into every card and
  leaves `product-flags` empty on a bottle nobody can buy, so its 1228
  listings all read as buyable -- including
  `winenot.fr/jura/3676-ploussard.html`, sold out for days, whose own
  `data-product` island says `quantity: 0`. A disabled add-to-cart decides
  whenever the page disables *some* and not all; that "some and not others"
  is the whole guard, because a control disabled on every card is furniture.
  The buy control is matched narrowly (`_is_cart_control`) -- cavepurjus
  ships a disabled hidden `orderby` input in a filter form, and reading that
  as stock suppresses a real bottle.
- `schema.org/InStock` is a claim, never evidence. Availability is read as a
  *value* (`href` or `content`, 1.6 and 1.7 spell it differently) and only
  ever believed when it says out of stock -- the same asymmetry
  `markup_says_sold_out` already documents. The vocabulary is wider than the
  obvious three, and `SOLD_OUT_AVAILABILITY` admits a value on one question
  only: can the reader put the bottle in a basket? `InStoreOnly` cannot be,
  so it belongs there -- a counter in Burgundy is not reachable from here,
  and a listing read as buyable is alerted once and then written to
  `seen.json`, which silences the restock. `PreOrder`, `PreSale` and
  `BackOrder` can be, so they must stay out: for a grower allocated in dozens
  of bottles a pre-order is often the only chance to buy, PrestaShop's own
  `allow_oosp` sells stock a shop does not hold, and this set can only ever
  *add* a sold-out verdict -- a wrong addition is a bottle that never reaches
  the inbox. Upstream is no help here: PrestaShop 1.6's `product.tpl` emitted
  `InStock` only when `quantity > 0` with no `else` branch at all (issue
  #13832, fixed only in 1.6.1.x), and 1.7 breaks availability again when
  stock management is disabled (#23199), which is why the buy button
  outranks the claim. vinnouveau's sold-out cards were
  held up by the French words beside them alone, and a restyle would have
  taken that shop silently to zero.
- The price is the one you would pay. PrestaShop renders a discounted card
  old-price-first ("Prix de base 13,50 € -10% Prix 12,15 €"), so taking the
  first currency-adjacent number recorded 13,50 -- 9 of pangee's 36 committed
  cards, and every card on both shops' promotions pages. A discount is
  exactly the news this scraper exists to send, so the defect reported a real
  DEAL as FAIR *and* fed the inflated figure to every other shop as their
  reference. Struck-through and "base" nodes are removed before parsing;
  never take the smallest number instead -- "729,90 € -40,00 € 689,90 €"
  would then be priced at the discount.
- A thousands separator is part of the number. `1 600,00 €` with a
  non-breaking space -- how PrestaShop writes every four-figure price -- was
  read as **600**, always downwards, always towards DEAL, and always on the
  dearest bottles watched. `MIN_CREDIBLE_PRICE` is the floor `pdflist` has
  always had: `€1 250,00` once parsed as 1.00, and a 1.00 reference makes
  every honest listing of that wine a HIGH for 180 days.
- A zero is not a price. Cart widgets ("Voir mon panier -- 0,00 EUR"), gift
  cards and "price on request" all carry a currency-adjacent zero, and zero
  is below every reference there will ever be, so such a row is a permanent
  DEAL -- one live dry run put exactly that in the digest. `positive_price`
  rejects it on all three platform paths, and `autoselect` does not treat a
  zero-priced block as a listing at all.
- Prices: parse currency-adjacent numbers only (a `€`, `$`, `£`, `EUR`, or
  `USD` marker touching the number). Never treat a bare 4-digit number as a
  price — it could be a vintage year. This is what `PRICE_PATTERN` /
  `parse_price` in `scraper.py` enforce; don't loosen it.
- A redirect is followed by re-entering `Crawler.get()`, never by requests.
  Every policy this class has -- robots, the per-host delay, the circuit
  breaker, the run budget -- is keyed on the host we *asked for*, and
  requests follows up to 30 hops silently. caves-carriere.fr redirects to
  www.caves-carriere.fr, so that shop was crawled against a robots.txt that
  was never read, and a chain across N hosts cost one delay and one budget
  unit. Re-entry puts each hop through all of it. It must stay *following*
  rather than refusing: six configured shops sit on a bare domain, and
  refusing would take them dark, which is a false negative. `MAX_REDIRECTS`
  bounds a loop, because neither the cache nor the breaker can -- every hop
  is a different URL.
- No module but `crawler.py` may call `requests` directly. If you're adding
  a new fetcher, it takes a `Crawler` instance and calls `.get()`.
- `evaluate.py` never suppresses a hit for missing/unverified reference
  data or low confidence — it classifies and flags a caveat instead. Don't
  add filtering there; that's exactly the behavior this scraper exists to
  avoid.
- The crawler's User-Agent identifies nobody. It is sent to every shop on
  every request and printed into Actions logs, which are world-readable on
  a public repo, so it carries no email, no personal name, no account name
  and no URL implying any of them -- the repo URL was rejected for exactly
  that reason. It also avoids the word "scraper", which some shops block on
  sight however politely the thing behaves. `Crawler.__init__` raises on a
  contact containing "@"; tests assert the default agent is `BOT_NAME`
  alone and that no workflow injects a contact. `CONTACT_URL` exists to opt
  back in to a contact that gives nothing away.
- Reference prices are observed from our own crawl (`market.py`), never
  fetched from a price aggregator. Wine-Searcher is blocked and against
  their terms -- never add code that fetches it. A hand-entered
  `reference_750_eur` is an *override*, only trusted ahead of observed data
  when `verified: true`; an unverified one ranks below observed data on
  purpose, because a guessed number produces a confident wrong verdict
  where no number produces an honest `NOREF`.
- The probe never accepts an HTML page on the spot. Every candidate is
  fetched and weighed, and the best `(paginates, count)` wins -- the landing
  page always going first, because that is where the shop's menu lives.
  Short-circuiting on "this looks like a catalogue" meant a recorded
  `catalog_path` (tried early by design) was taken before the menu had been
  read, so a wrong path confirmed itself on every re-probe: three runs left
  winenot on its sparkling-wine filter. A JSON platform still ends the
  search, because `/products.json` either is the catalogue or is not there. Product count alone chose
  pangee's "new arrivals" strip over its catalogue and winenot's
  sparkling-wine filter over nine region categories -- a strip is one page, a
  catalogue runs to twenty. When several categories each hold part of the
  catalogue and none holds all of it, they are recorded as `catalog_paths`.
- A new HTML shop does not get hand-written selectors by default. Let
  `autoselect` try first; only write per-shop selectors when the probe
  shows it genuinely cannot read that page. Selectors are a maintenance
  cost paid per shop, per restyle.
- A producer who sells several ranges under one surname needs bands, not a
  pooled average. Ganevat bottles the domaine's own Cotes du Jura (~EUR 91),
  a negoce line with his sister Anne from bought Jura fruit, and a negoce
  line from fruit outside the Jura (~EUR 40 or less). One pooled reference
  makes the cheap bottle a permanent DEAL and the dear one a permanent HIGH,
  which is what `lines:` in `prices.yaml` exists to stop: a per-line
  threshold, absolute, **per 750ml equivalent** so a magnum is not a bargain
  for being big and a clavelin -- 620ml and almost always vin jaune, dearer
  by nature -- is not one for being small. It is a deliberate exception to
  "references are observed": a human who knows the range set these, and the
  observed pool cannot separate what a label does not distinguish.
  A band gives three verdicts, not two. DEAL is strictly under
  `deal_under_750_eur`, and stays there: that number is a human's deliberate
  threshold, so the band path keeps a different DEAL rule from the market
  path's `deal_threshold` of 0.85, on purpose and documented rather than by
  accident. HIGH is above `high_over_750_eur`, defaulting to 1.5x the band.
  Without it the path had no HIGH branch at all and a Ganevat at EUR 2000
  against the EUR 80 domaine band classified FAIR at a ratio of 25, where the
  same ratio on the market path is HIGH -- so for the producer watched hardest
  an overpriced bottle read exactly like a correct one.
  A `styles:` map picks *which band a line is judged against*, and never which
  line the bottle is in. Vin jaune, Chateau-Chalon and macvin are made by both
  ranges, which is why they must stay out of the curated cuvee lists; as
  cuvees they would outrank the label and file a negoce bottle under the
  domaine. A style is read after the line is settled, so it cannot. It exists
  because a style dearer by nature starts above a band derived from the whole
  range: a 620ml clavelin at EUR 118 is EUR 142 per 750ml against an EUR 80
  band, so vin jaune could only ever read FAIR. Vin de paille is deliberately
  not a style -- it is usually 375ml and shops rarely say so, and an unstated
  size defaults to 750ml, so the band would be compared against what is
  probably a half bottle.
  A line may be configured `alert: false`. Such a hit is classified `NOALERT`
  and stays out of every email body -- digest, recap and on-demand alike,
  because a recap of "everything currently matched" would otherwise put it
  back in the inbox weekly -- while still being written to `hits.json`.
  Never make `evaluate.py` *drop* it: silencing an alert is notify's decision,
  and the row is the only way the human sees a cuvee that needs classifying.
  A bottle that cannot be placed goes to `negoce_unclassified`, which never
  alerts: an unplaced cuvee is more often negoce-from-outside than not, and
  guessing the band is how the false deals came back.
- Which line a bottle belongs to is decided by the curated cuvee list first,
  the label's attribution second. Shops file negoce cuvees under "Domaine
  Ganevat" often enough that the label is not the last word, and the
  attribution mark is matched as a whole word -- "COFFRET ANNIVERSAIRE
  GANEVAT" is a real listing and "anniversaire" is not "Anne".
- `market.segment()` normalises "&" to "et" before splitting, and treats
  format words as generic. Both were live bugs: "Anne & Jean-Francois
  Ganevat" lost its "Anne" at the segment boundary and keyed *identically to
  the domaine*, while "Anne et Jean-Francois Ganevat" keyed apart -- so which
  price pool a negoce bottle joined depended on the shop's typography, and
  mareehaute writes it both ways in one catalogue. "... 2021 Magnum -
  Jean-Francois Ganevat" keyed as `magnum jean francois ganevat`, a third
  line for one producer, so a cuvee's magnums were never compared with its
  bottles.
- `market.py` must keep cuvee, vintage and format in the comparison key.
  Collapsing any of them re-creates the bug the module exists to fix: a
  EUR 30 negoce bottle scored against a EUR 70 domaine average reads as a
  permanent DEAL, and a EUR 450 coffret as a permanent HIGH.
- A wine seen at no other shop gets no reference. Do not add a fallback
  that invents one -- `NOREF` is a real answer and the hit is still
  reported.
- A restock is the alert this scraper exists to send, and it needs presence
  timestamped. Nothing about a sold-out listing is persisted -- but nothing
  *erases* its `seen.json` entry either, so a bottle returning inside 30 days
  was not new, had not dropped 10%, and was inside the cooldown: silent. For
  growers allocated in a few dozen bottles that gap is the normal case, so
  the promise was kept only for a bottle gone over a month. `last_seen_at` is
  written for every **in-stock** hit -- a sold-out listing still writes
  nothing -- and a return after `ABSENT_DAYS` alerts, cooldown-exempt for the
  same reason a drop is. The load-bearing guard is the shop's *previous*
  successful read (`_came_back`): after a shop is unreachable for a week every
  listing on it looks absent, and announcing its whole shelf as restocked is
  a lie. One run re-stamps them quietly first.
- Every row says why it is there. `alert_reason` distinguishes new / drop /
  back / deal, and the reason reaches the email -- a bottle first seen at
  EUR 60 used to render byte-identically to one that fell from EUR 100.
- Losing the observed price pool is silent by construction:
  `market.load_observations` returns an empty store for a missing file and a
  corrupt one alike, so the run is green, emails on schedule, and classifies
  everything `NOREF` -- which from the inbox is a quiet week. `main()` states
  the carried-in count every run and notes it when a pool arrives empty.
- The run's result goes to `$GITHUB_STEP_SUMMARY` as well as the artifact.
  `hits.json` expires in 30 days and cannot be opened from a phone; answering
  "is this bottle in the results?" once cost a twelve-minute live re-crawl.
  Display only -- it marks nothing alerted and touches no state.
- `scraper.yml` and `discover.yml` run `permissions: contents: read` with
  `persist-credentials: false`. They are the two jobs that feed third-party
  HTML, JSON and PDFs through bs4 and pypdf, and neither pushes anything, so
  a parser bug or a bad dependency release must not come with a token that
  can write to `main` -- which is what the next scheduled run executes.
- A sold-out match is remembered, never alerted. `check_shop` matches
  every parsed listing and puts out-of-stock matches on
  `ShopResult.sold_out`; they must stay out of `hits.json`, out of the
  market pool and above all out of `seen.json`. That last one is what makes
  a **restock** read as a new item and alert — the most valuable alert this
  scraper sends, and it works because nothing about a sold-out listing is
  persisted. `tests/test_coverage.py` asserts both halves; don't "improve"
  it by recording sold-out state.
- A document states stock in a column, and the column must be read. Two
  shops now publish their range as a PDF and they are not the same document:
  purewijnen writes a ragged wine list where `SOLD` replaces the price,
  wineshopbiarritz exports its inventory with a `Stock` number in front of
  it. Read as the first, the second reported 1546 wines and **not one** sold
  out while its own column said 0 against 707 of them — every Richard Leroy,
  the Pierre Overnoy, all three Gauditiabois — which would have alerted them
  as finds and written them to `seen.json`, silencing the restock that is the
  point of watching them. Only a **zero** is read as stock: the column is
  unlabelled once the PDF is flattened, so any other number could be a
  vintage or a case size, and guessing is how a wine goes missing. See
  `pdflist.ZERO_STOCK_BEFORE_PRICE`.
- A running header is furniture, and it is recognised by **where it sits**,
  not by how often it repeats. `FURNITURE` cannot know the wording of every
  list — it knew "Naam Streek" and not "Nb Nom Famille Sous Stock Prix", so
  biarritz's header buffered like a producer name and titled three Ganevat
  rows. Judging it by recurrence alone is worse: a grower's name recurs once
  per wine, and that rule dropped `Renaud Bruyère-Houillon` out of
  purewijnen's list entirely. `_running_furniture` takes the *pages* and
  looks only at each page's first and last few lines; given flat text it
  finds nothing rather than guessing.
- A document entry's title carries no price and no stock count. `notify`
  keys a remembered item on its URL, and a document item's URL fragment is
  built from its title — so leaving the numbers in minted a new key on every
  price change, and a price *drop* could only ever be reported as a new find.
- Which Ganevat line a bottle belongs to is decided by the curated cuvee
  list, then the label — and the label includes the word `negoce`, not just
  the `anne` attribution. A shop that files its range as "GANEVAT DOMAINE x"
  and "GANEVAT NEGOCE y" is telling us outright, and ignoring it put 20 of
  biarritz's negoce bottles against the domaine's EUR 80 band, most of them
  flagged DEAL. Style words are not cuvees: `vin jaune`, `macvin`, `vin de
  paille` and `cremant` are made by both ranges, so they must stay out of the
  curated lists — as "cuvees" they outrank the label, which is how three
  bottles of "GANEVAT NEGOCE VIN JAUNE" were scored as domaine.
- "Watched but found nowhere" means matched nowhere at all, in stock or
  not. A producer stocked somewhere but sold out belongs in "Matched but
  sold out everywhere" with its shops named; collapsing the two back
  together restores the exact ambiguity the note was added to remove (one
  run called 13 of 16 producers missing while a single shop hid 2135
  sold-out listings).
- Near-misses are suspicions, not findings: only for producers that matched
  nothing, only alias tokens of 7+ characters, only at edit distance 1, and
  only when *neither* side is a word the corpus shows at more than one shop.
  That last filter is doing the real work -- two live runs offered
  `'pierres'`, `'pierra'`, `'pierro'` and `'malice'` before it existed,
  because "pierre" and "calice" are French before they are estates.
  Loosening any of it turns the note into noise, and a noisy note is an
  ignored note.
- When a bare surname is genuinely needed, the namesake carries the
  distinction. `NAMESAKES` is matched exactly like `PRODUCERS` and then
  discarded, so a longer namesake alias takes the bottle away from the estate
  we watch and reports nothing. "Labet" cannot become "domaine labet" --
  mareehaute writes real Jura cuvees as "Chercheurs d'or 2009 Labet" -- while
  caves-carriere carries LABET PIERRE, LABET FRANCOIS and a "Clos-Vougeot
  Grand Cru ROUMIER LAURENT 2021", none of them ours. It is defined **above**
  `PRODUCERS`: `apply_issue.py` finds the end of that dict by the "Shops to
  check" banner, so a dict in between silently swallows every producer the
  issue form adds, as a name that is matched and then dropped.
- A bare surname may be trusted at one named shop, through `SHOP_ALIASES`,
  and that is the only way it may ever be trusted. winenot lists "PACK
  OVERNOY SAVAGNIN 2018" at EUR 580, in stock, and that title is the whole of
  what the shop says -- no "Pierre", no "Houillon" -- so the estate we watch
  was invisible there while its bottle sat on the shelf. The scope is the
  shop *name*, so it cannot leak: `probe.py` and `discover.py` pass no shop
  at all, and neither should decide a candidate shop is readable on the
  strength of a surname. Three things make the trade survivable and all three
  must hold -- the namesakes are in `NAMESAKES` so a longer alias still takes
  the bottle away (adding a scoped surname without them is how
  Overnoy-Crinquand gets reported as Pierre Overnoy), the alias that fired is
  named in the digest row, and a human judged that shop's range. "Domaine
  Overnoy" is deliberately *not* a namesake: shops write Pierre Overnoy's own
  estate that way, so blocking it would silence the real bottle. Adding to
  this table is a watchlist decision, not a fix for a shop that parses badly.
- Producer aliases must name an estate, not a surname. `match_producers`
  prefers the longest matching alias, but that only separates producers we
  track -- it cannot help against an untracked namesake. Jura and Savoie
  are full of them: "overnoy" alone also matches Domaine Overnoy,
  Overnoy-Crinquand and Overnoy Jean-Louis et Guillaume; "houillon" also
  matches Corentin Houillon and Fimbel-Houillon; "brochet" also matches
  Emmanuel Brochet. Every one of those was reported under the wrong
  producer from real catalogues. Use the full name.
- Text comparison goes through `textnorm`, never a local copy. Use
  `strip_accents` for anything a price, vintage or cru regex reads
  afterwards, and `match_key` only where a *name* decides a match
  (`scraper.matched_aliases`, `apply_issue`'s derived alias — those two must
  agree or a producer added through the form matches nothing). Putting
  `match_key` in front of `market.VINTAGE_RE` turns "2018,50 €" into
  "2018 50" and a price becomes a vintage.
- A sold-out marker in *every* card is decoration, not stock.
  leszinzinsduvin's theme ships "Produit épuisé" inside all of them and
  reveals it with CSS, so the text condemned the whole shop -- 12 products,
  0 in stock, every run -- while three of the four cards in its capture have
  an enabled buy button and one does not. `_stock_reader` believes the
  buttons only when the text has disqualified itself by condemning every card
  *and* the markup distinguishes some from others; both halves are required,
  because a category page really can be sold out top to bottom and calling it
  available alerts on bottles nobody can buy, then writes them to `seen.json`
  and silences the restock. This failure suppresses finds rather than adding
  noise, so nothing reports it: the shop simply looks quiet.
- A listing that is sold out is not a find. Stock comes from the platform
  when it answers (`available` on a Shopify variant, `is_in_stock` on the
  WooCommerce Store API) and from the listing text otherwise ("epuise",
  "rupture de stock"). `in_stock` is set by the fetchers and dropped by
  `check_shop`, never by the parser -- the probe counts parsed products to
  decide whether an adapter works, so a shop whose stock is out today must
  not read as broken. Silence from an API is not "sold out".
- `notify.py` sends at most one email per run -- a digest, a recap, or an
  on-demand report, never two of them, and never one per hit. Don't
  reintroduce a per-hit email path.
- A hand-started run always answers. `FORCE_REPORT` is set by the workflow
  for `workflow_dispatch` and nothing else: the hourly schedule stays quiet
  without news, but a dispatched run emails everything currently matched
  even when the list is empty -- that empty table is the only way to tell
  "nothing new" from "credentials expired" from a button. It marks nothing
  as alerted, exactly like the recap, and `DRY_RUN` still overrides it.
- The row cap must never silence a hit it did not show. An email carries at
  most `EMAIL_ROW_CAP` rows, and marking an item alerted is what silences it
  for 30 days: one live run had 79 alert-worthy hits, printed 40 and marked
  all 79, so 39 real finds reached nobody and then went quiet for a month with
  only the `hits.json` artifact as evidence. The overflow is held back
  unmarked (`_hold_back`) and leads the next digest, and the note says how
  many. Restore the *whole* previous entry when holding one back -- a
  half-entry with `last_price` but no `last_alerted_at` is not new, is past no
  cooldown, and can only ever re-alert by becoming a `DEAL`, which loses a
  held-back `FAIR` for good.
- `notify.py` persists `seen.json` only *after* the email is actually sent.
  Marking an item alerted is what silences it for 30 days, so saving before
  the send means a dry run or a failed send silently swallows a real find.
  Never move `save_state` back above `send_email`. A `DRY_RUN` writes no
  state on any path, including the silent one.
- The weekly recap must never mark an item alerted. It is not a find, and
  marking one would silence a real drop for 30 days; it refreshes
  `last_price` only, exactly as a silent run does. It is also not a
  heartbeat -- a run with no hits at all still sends nothing.
- A bottle size is read most-specific-first, and a sweetness is not a size.
  "Double Magnum" contains "magnum", so with 1500 tested first a 3L bottle
  was recorded at price/2.3 instead of price/5.0 -- 2.2x too high, for the
  180 days an observation lives. "Demi-Sec" contains "demi": pangee's "Vin
  Blanc Demi-Sec" and winenot's "Atemporelle Demi Sec" were read as 375ml at
  *high* confidence, so a full bottle entered the pool at price/0.55 and
  scored itself against expected = reference x 0.55, a DEAL with no caveat.
  A bare "demi" still means a half bottle; only the sweetness words
  disqualify it.
- A pack is a bundle. `pack`, `lot de N`, `duo` and the "5 +1 offerte"
  promotional shape join coffret and caisse in `BUNDLE_RE`: pangee sells
  "Le Fruit blanc 2024 ( 5 +1 offerte )" at EUR 36, six bottles priced as
  one, and a live run named "Ganevat: Pack" among its unplaced cuvees. Priced
  per bottle that is EUR 6 against a EUR 13 reference, which is a guaranteed
  DEAL, and the row entered the reference pool as a single bottle.
- A coffret/caisse is several bottles, so its price is not comparable to a
  per-bottle reference. `evaluate.py` must keep detecting bundles, applying
  no format multiplier, and always caveating them -- real listings like
  "COFFRET ANNIVERSAIRE GANEVAT" at EUR 450 would otherwise be scored
  against a ~EUR 70 bottle reference and shouted about.
- Silent failure is the enemy, so `main()` states three things it used to
  keep to itself. `check_shop` returns a `ShopResult` (a `list` subclass,
  so its fifteen callers are unaffected) carrying `products_parsed`: a
  verified shop's fixture always parses to more than zero, so zero from a
  live fetch is adapter drift and gets a `DRIFT` line plus a digest note.
  It is reported, never escalated to a failed run -- a shop can be empty
  for a night, and an hourly red run teaches you to ignore red runs.
  Producers matched at no shop are named too: an alias typo makes a
  producer vanish from every shop at once, which is otherwise invisible.
- Every run states its coverage: one row per live shop with pages read out of
  the catalogue's own stated total, products read,
  in stock, sold out, hits and the producers matched, in the log, in every
  email and in `coverage.json` (uploaded with the artifact). It exists to
  answer "does what we read match what the shop actually sells", which was
  previously only answerable by reading a log line by line. A shop that
  failed still gets a row -- a shop missing from the table entirely is how
  "we never looked" hides. `STATUS` is `TRUNCATED` when the walk stopped
  before the catalogue did (`ParsedItems.truncated`, set by `_paged` and
  `fetch_html`), and that is the one cell that decides whether the row can
  be compared with the shop's real selection at all.
- The schedule is not a promise GitHub keeps. A public repo's scheduled runs
  are queued at low priority: `cron: "0 * * * *"` delivered runs at 20:22,
  22:08, 23:55, 02:24, 05:43, 08:27, 11:17 and 13:55 -- and then two runs were
  **cancelled without ever being given a runner** (`runner_id: 0`, no logs, a
  15-minute wait then cancelled), which arrives as a failure email that looks
  exactly like a broken scraper and is not one. Asking for 24 runs to receive
  10 only adds queue pressure, so the schedule asks for what it can get, and a
  `concurrency` group keeps a delayed run from overlapping the next one.
- Every outbound request counts against the budget, robots.txt included. It
  was not counted, so a run made about one uncounted request per host --
  roughly 19 against a 400 budget sized from a measured 311-request pass --
  and `MAX_REQUESTS_PER_RUN=1` still went to the network, which left the
  budget untestable at its own boundary. It is deliberately not behind
  `_wait_for_host`: robots.txt must be readable before that host's
  `Crawl-delay` is known.
- `Crawl-delay` is honoured upwards only. `or MIN_DELAY_SECONDS` let a shop
  publishing `Crawl-delay: 1` pull us *below* the 3s floor this file
  documents, which is the opposite of honouring the header.
- The connect timeout is separate from the read timeout. A single `timeout=`
  applies per socket read, so a host dribbling a byte every 14 seconds holds
  the connection indefinitely -- and `MAX_RUN_SECONDS` is only checked
  between shops, so one such host runs the job past `timeout-minutes` and
  loses the whole crawl: no `hits.json`, no email, a red run, no explanation.
- The run stops itself on wall clock as well as on requests. Reading all 23
  shops to the end of their catalogues is 311 requests and ~28 minutes cold
  (vinnouveau's 118 pages are most of it); a job killed at the runner's
  ceiling loses the whole crawl -- no `hits.json`, no email, a red run and no
  explanation. `MAX_RUN_SECONDS` (default 2700) breaks the shop loop, names
  the shops not reached, and lets the run report what it has. The request
  budget (400) must still bind *before* the clock, because the clock is only
  checked between shops and so drops whole shops, where the budget degrades
  one catalogue and marks it TRUNCATED. `tests/test_budget.py` holds that
  ordering -- budget inside clock, clock inside `timeout-minutes` -- as a
  relationship, not as today's numbers. Most runs cost far less than a full
  pass: a cache hit returns before the budget check, so with a 6h TTL the
  crawl is paid four times a day.
  `timeout-minutes` in the workflow is only the outer backstop and must stay
  well clear of it.
- `shop_order()` rotates SHOPS by *hours since the epoch*, not by the hour
  of the day. The budget is global and was spent in list order, so the moment
  it binds it is always the same tail that goes unfetched -- systematic, not
  random. `hour % len(shops)` looks like it fixes that and does not: with 29
  shops only 24 offsets ever occur, so five shops could never lead a run.
  All three "not reached this run" messages must index the rotated order, not
  `SHOPS` -- there are three, and the pre-fetch budget check is the one that
  gets forgotten.
- A shop the run never reached still gets a coverage row (`not reached`), and
  "watched but found nowhere" is only that when every shop was read.
  Otherwise a binding budget silently converts "we never looked" into an
  accusation against the aliases, which is the opposite of what that note is
  for.
- `catalogue_starts()` pins the measured-best catalogue first and rotates
  only the rest. One page budget is shared across every `catalog_paths`
  entry, so a fixed order spent all of it on the first category -- winenot
  had never read its rosé, sparkling, moelleux or muté at any budget (it now
  reads one union filter instead; pangee still carries a list).
  Rotating everything is not the fix either: the probe also records pages
  that merely parsed (a portfolio page, a sub-category), and giving those
  first place spends the budget on a slice of the shop instead of the whole
  of it.
- A catalogue is not always the whole shop, and being first is the point.
  winenot's catalogue is a `Couleur / Type` facet union, so a listing carrying
  no colour is not in it: the same probe run found **0** titles containing
  "pack" across the crawled catalogue and **15** on `/nouveaux-produits`,
  including `4963-pack-overnoy-savagnin-2018` at EUR 580 and
  `4979-pack-labet-la-reine`, both in stock and neither ever reported. An
  optional `new_arrivals` path on a SHOPS entry is walked after the catalogue
  with a fresh seen-set, capped at `NEW_ARRIVALS_PAGES` because a new-arrivals
  strip is the newest N products and not a second catalogue, and deduplicated
  against the catalogue's URLs. What is left over is `off_catalogue`, and it
  is a *number the digest states* -- "read incompletely" -- because a shop we
  read partially otherwise prints a clean coverage row. It is a mitigation,
  not a cure: the cure is finding the path that states the whole range.
- A first-pass failure is not evidence a shop is down. A shop reported
  `unreachable` contributes no digest rows, nothing to the price pool, and a
  "watched but found nowhere" line its aliases did not earn -- and vinnaturel
  answered a hand probe with a healthy 66KB catalogue minutes after a run had
  written it off. `main()` retries `UpstreamError` and `CircuitOpen` **once**,
  after every other shop is read, calling `Crawler.reopen()` to clear the
  breaker for that host alone: by then it has had twenty quiet minutes, which
  is the pause a rate limiter is asking for. Nothing else is retried --
  `Challenged`, `Disallowed`, an empty JS storefront and a parse error all say
  the same thing twice, and a challenge is a shop saying no. Neither is a
  status that *answered*: `NO_RETRY_STATUSES` keeps 401/403/404/405/407/410/451
  out of the second pass, because mesbourgognes answered HTTP 403 to every
  request of a run and that is the sentence demainlesvins said before it was
  removed.
  A refusal also gets its own word in the coverage table (`refused 403`) and
  its own digest note: reported as "unreachable" it reads as a network fault
  someone could fix, which is how a shop that has closed its door stays on the
  list looking like an outage. Two guards are
  load-bearing: the retry is skipped entirely when the first pass left shops
  unreached (what budget remains belongs to a shop nobody has read once), and
  a recovered shop **replaces** its failure row in `coverage` rather than
  appending, or one shop is counted twice in every total. Recovery goes
  through the same `absorb()` the first pass uses -- a shop read on the second
  attempt must reach the digest, the market pool and the sold-out note by
  exactly the same route -- and is named in the digest as "Failed once, read
  on retry", because a flaky shop otherwise prints a clean row until the day
  the retry fails too and it looks newly broken.
- A group is worth what it holds in *distinct* wines, and only then in
  priced blocks. winenot's `/nouveaux-produits` carries its real grid --
  `#js-product-list > .products.row`, four cards, three of them the packs no
  catalogue of that shop lists -- beside a cross-sell carousel rendering the
  same three Cortons *twice*. Six priced blocks beat three, so the carousel
  won every run: the strip was walked, parsed cleanly, added nothing, and the
  log said so in no line at all, while PACK LABET LA REINE sat there in stock
  at EUR 290. Six blocks for three wines is three wines, which makes it 3
  against 3, and the tie goes to the parent describing more products, priced
  or not -- the carousel points at 3 pages, the grid at 4, because its
  priceless fourth card is still a card. Asking the markup which of them is
  the grid beats asking which came first in the document, which is all `max`
  over equal counts was doing.
  `tests/fixtures/winenot-new-arrivals-excerpt.html` keeps both groups as
  real markup.
- The new-arrivals walk states its result on every outcome, including the
  boring one. Its first live run added nothing and printed nothing, so "the
  path is wrong", "the page is unreadable" and "the catalogue really does
  hold everything" were indistinguishable from the log. The count is only
  news when it is not zero; that it was *measured* is news every time.
- One dead category must not black out a shop. `_walk_pages` raises when
  *page one* fails, which is right for a shop with one catalogue and wrong for
  a shop with six: a renamed category would take the whole range dark. A
  failing start is now only the shop failing when *every* start failed;
  otherwise it is logged, the row is marked TRUNCATED, and the rest are read.
  `catalogue_starts` pins the measured-best catalogue first, so this degrades
  in the right order.
- The bottles we have found are remembered by URL, because nothing else
  remembered them. `observations.json` keeps `(producer, seg, shop)` and no
  URL at all, `seen.json` keeps only a `sha256(shop|url|variant)`, and
  `hits.json` is not carried between runs -- so when a shop's walk was cut
  short, the few dozen URLs we actually care about went unchecked and nothing
  said so. `watchlist.json` (gitignored, carried by `actions/cache`, pruned on
  the same 180-day window `market.merge` uses) is written from the **full
  pass only**: a hot pass reads one surface per shop, so the URLs it did not
  see are not absent -- they were never looked for, and pruning on that basis
  would forget the whole list.
  `recheck_watchlist` re-reads them through `detail_price`, the same
  product-page reader `_price_from_detail_pages` uses, and only for a shop
  whose coverage row says `TRUNCATED`: a shop that refused us, was blocked or
  was never reached is not somewhere to spend what little is left, which is
  the trade the retry pass already makes. The coverage row is left alone
  because it describes the catalogue walk and this is not one; what the
  re-read found gets its own digest note. Two things are load-bearing: the
  producer is **re-derived** from the stored title by `matched_aliases` rather
  than trusted from the entry, because a remembered attribution would outlive
  an alias being corrected -- the exact bug `NAMESAKES` exists to prevent --
  and a re-read that finds `quantity: 0` goes to `sold_out`, never to hits, so
  it stays out of `seen.json` and its restock still reads as new.
- Every state file must be in **both** workflows' `actions/cache` path list,
  even the ones that workflow never touches. `actions/cache` saves only the
  paths it is given and the full pass restores the most recent matching
  entry -- which is usually a hot run's. Leaving `watchlist.json` out of
  `hot.yml` would quietly drop it from every hot entry, so the next full pass
  would restore a state without it and rebuild from nothing. A new state file
  also needs `.gitignore`: the suite writes through these paths, and one went
  into the repo full of `zzz-shopify` rows before `conftest.py` redirected
  `WATCHLIST_PATH` to `tmp_path` like the other four.
- "No grid" is not "no data". A Wix Stores page serves the product *links*
  and renders the grid in the browser from a JSON island, so `autoselect` --
  which finds a card by its price -- sees nothing and the probe reports
  "responded and parsed, but zero products found". feralart read that way on
  every path while its `/shop` page held 40 currency markers, 25
  `/product-page/` links and an island stating 12 products. `wix_products`
  finds `"productsWithMetaData":{"list":` and matches brackets to the end of
  the array -- not a regex, because descriptions contain brackets and escaped
  quotes -- and `_fetch_via_wix_island` reads it from the page already in
  hand, costing no request. It is a *better* source than a grid: `price` is
  numeric, `currency` is named, and `isInStock` is a flag rather than a French
  word to infer from. This is the demainlesvins `data-product` lesson in a
  different costume, and it is tried before the document reader because it is
  free.
  **Only the `name` field reaches the matcher.** feralart sells books and
  candles whose blurbs name growers: its real "A Vif - Terroirs en Feu"
  publication describes itself as "covering Julie Balagny, L'Arbe Blanc,
  **Labet**, Aurelien Lefort…", and `matched_aliases` returns Labet for that
  sentence. Matching descriptions would file a book as a bottle of Labet at
  the book's price -- a blog post about Overnoy is not a bottle of Overnoy,
  and neither is a book about Labet. `comparePrice` is never read either: Wix
  puts the struck-through figure there and `price` is the one you would pay.
  `tests/fixtures/feralart-wix-island-excerpt.html` keeps three of the shop's
  twelve records verbatim.
  **An island is one page of a catalogue, and reading it is not reading the
  shop.** feralart's states `"totalCount":923` next to the twelve records it
  lists, so the first version of this reader took 1.3% of a shop and reported
  a clean coverage row -- "catalogues are paged" in a new costume, and the
  worse kind, because none of the twelve is a producer we watch and the row
  would have said `ok` for ever. How far to walk is derived from the total the
  island states, exactly as `autoselect.catalogue_size` derives it, and never
  from a pager link: Wix renders its pager in the browser too, so
  `find_next_page` finds nothing and a walk waiting for a `rel="next"` reads
  page one and stops. `?page=N` is the shop's own parameter -- `/shop?page=2`
  serves twelve different wines and titles itself "2/77". Three details are
  load-bearing. The page size is the count of **records**, not of the items
  the reader kept, or a skipped nameless record shortens every page and the
  walk asks for pages that do not exist. `totalCount` is read only from the
  metadata **immediately after** the record array, because a Wix page carries
  other counted widgets and a review count read as the catalogue's size sets
  the walk's length from a number about something else. And an island stating
  no total reads the page in hand and **says so** rather than guessing at a
  page two -- `None` is a real answer here as everywhere.
  The exception ladder is the one `_walk_pages` documents, for the same
  reasons: `UpstreamError` on a later page ends the catalogue without
  truncating it (a platform 404ing the page past its last is the catalogue
  ending, and folding that in marks such a shop permanently TRUNCATED),
  everything else that stops the walk early *is* truncation and must say so,
  and **nothing re-raises** -- the catalogue page is already in hand and
  nothing a supplementary reader does may cost it.
- Generating the page is not publishing it, and the gap is silent.
  `wine.html` is committed by `dashboard.yml`; GitHub **Pages** deploys it in
  a separate run. When a Pages deployment wedges, every later one is refused
  with "Deployment request failed ... due to in progress deployment" -- and
  `dashboard.yml` goes on reporting success while the live page stays frozen.
  On 2026-11-01 it stayed frozen across four rebuilds and two merges; the
  owner pressed "Rebuild this page" repeatedly and the only symptom was a
  button that would not appear, because the published copy was the build from
  `5579c07`. `dashboard.yml`'s last step now reads the most recent `pages
  build and deployment` conclusion and fails loudly on `failure`. It reads the
  *previous* deployment, since this push's has not started -- which is the
  right question anyway, because a wedge is a standing condition.
- State lives in two caches, and the split is load-bearing. `seen.json`,
  `observations.json` and `watchlist.json` are a few hundred KB; `.cache` is
  ~6.5 MB of fetched pages. Cached under one key they are evicted under one
  key -- and run 41290903522 restored nothing, so the pool arrived empty, the
  POOL note fired, and every verdict that run rested on a single crawl.
  Losing the HTTP cache costs requests and nothing else; losing `seen.json`
  re-alerts every bottle and then silences each for 30 days, and **the hot
  pass depends on it outright**, because it alerts on URLs *absent* from that
  file -- with no state it would mail the whole shelf every hour. So
  `wine-scraper-state-` holds the three small files and
  `wine-scraper-httpcache-` holds `.cache`, in both workflows.
- Being first is the point, and the cache TTL is the lever -- not the cron.
  A full pass is ~410 requests and 11-32 minutes against a 450 budget, so it
  cannot run often enough to win a race for a bottle that exists in dozens,
  and every catalogue page it reads is served from the 6h disk cache, so even
  a run that fires can read a page that predates the release. `HOT=1`
  (`.github/workflows/hot.yml`, hourly) reads one surface per shop at
  `FRESH_PAGE_TTL_SECONDS` instead: `hot_surface()` admits a Shopify shop
  (its `/products.json` doubles as a recency feed) or an HTML shop with a
  configured `new_arrivals` path, and `fetch_hot()` reads that and nothing
  else -- measured at 39 pages and 3m48s over the 10 shops that had a surface
  (run 41305306786), most of it mareehaute's 16 pages, because `fetch_hot`
  reads Shopify's `/products.json` in full rather than guessing at its order.
  The dashboard has a **Run hot pass** button beside the full one; the
  dry-run checkbox applies to either. A shop with neither
  surface is **not read at all** rather than read badly; a partial catalogue
  walk hourly would spend every shop's politeness budget to learn nothing the
  full pass will not learn properly. It goes through `check_shop(fetch=...)`,
  so alias matching, the sold-out split and near-miss tokens are the same
  code: a hot find reaches the digest by the route a normal find takes.
- **A hot pass alerts only on a product URL never recorded before, and that
  filter is a safety mechanism before it is a policy.** `_update_state`
  stamps `last_seen_at` for *every* in-stock hit, alerted or not, and
  `_came_back` refuses to call a bottle "back" when that stamp is newer than
  `ABSENT_DAYS`. So a hot pass that merely *looked at* a bottle it already
  knew would restamp its presence and silently destroy the restock alert --
  the one this file calls the most valuable alert the scraper sends, and the
  normal case for a grower allocated in a few dozen bottles. Handing
  `notify.run_digest` only hits whose `item_key` is absent from `seen.json`
  means nothing known is ever passed in, so nothing known can be restamped.
  Drops, restocks and reclassifications stay with the full pass, which has a
  whole catalogue to judge them on. It also passes `shops_read=()`: that
  argument is what stamps `_meta.shops`, the "were we actually looking" guard
  behind `_came_back`, and reading a strip is not reading the shop.
- A hot pass must not say anything that only a complete crawl can know. It
  writes no `coverage.json` (a row per shop describing one page is not a
  coverage table), no `observations.json` (the pool is reference data and a
  strip is a biased slice; a new bottle's reference comes from *other* shops
  anyway), no `DRIFT` line (a strip with nothing on it is a shop that
  released nothing today), no "watched but found nowhere", no "sold out
  everywhere" and no alias near-misses. Each of those printed hourly off one
  page would be an accusation every hour, and a word that cries wolf stops
  being read. The one thing it keeps is `FORCE_REPORT`: a run a human pressed
  a button for still answers with an empty table, because silence from a run
  you asked for reads as a dead scraper.
- Both workflows share one `actions/cache` entry, so they share one
  `concurrency` group. `hot.yml` deliberately uses `group: wine-scraper`,
  the full pass's group, rather than one of its own: seen.json decides what
  has already been alerted, and two runs restoring and saving the same cache
  entry at once means the loser's writes vanish. Queueing behind a 30-minute
  full pass costs a hot run its slot; a lost state write costs a real find.
- Shopify states its own release time, in a payload we already fetch. Every
  product in `/products.json` carries `published_at` and `created_at`, and
  `fetch_shopify` discarded both for months -- so six verified shops can date
  a listing exactly, at zero extra request cost, and the row for a bottle
  listed four minutes ago rendered identically to one listed last month.
  `listed_at` is carried through `check_shop` (which builds its row key by
  key, so it has to be named there), rendered by `notify._listed_age`, and
  used by `order_hits` to lead each classification section with the freshest
  bottle. It is honest in the other direction too: a listing the shop
  published three months ago, arriving as a *new* find, says our detection
  was late rather than that the wine is fresh. No HTML shop states anything
  comparable, so it is absent there -- rendered as nothing, never guessed,
  and never sorted to either end of time.
- `fetch_hot` reads Shopify's `/products.json` **in full**, not page one.
  Shopify does not document that ordering as newest-first, and assuming it
  would quietly drop a release onto a page nobody asked for. At 250 products
  a page these shops cost a request or two, so there is nothing to buy by
  guessing -- recency comes from `published_at`, a fact the payload states,
  rather than from an order we inferred.
- The new-arrivals strip is fetched **first** and merged **last**. It used to
  be both last, which made the freshest page in the system the first thing a
  binding budget or clock took away -- precisely backwards, because a
  catalogue cut short is a TRUNCATED row that says so while a strip never
  read is a bottle nobody hears about. The merge stays after the catalogue
  and the producer index because `off_catalogue` means "listings the
  catalogue does not carry", and that number only means what it says once the
  catalogue is already in `items`.
- A release feed is a fact to be read, not a URL pattern to be guessed, and
  two obvious ones were checked and rejected. **Sitemaps**: pangee advertises
  one in robots.txt and it is a real `<sitemapindex>`, but its single
  sub-sitemap's `<lastmod>` reads 2024-02-06 -- about two years stale.
  **Sorting a catalogue by newest**: winenot and pangee both publish
  `Disallow: /*?order=` and `Disallow: /*&order=`, so that URL is a shop
  saying no. Neither is a recency signal here, and a guess about what a
  platform publishes is not evidence -- read the file.
- A failed page is not a failed shop, and the handlers say so by naming
  `crawler.FetchError` rather than a tuple of its subclasses. `get()` raises
  five things -- `Disallowed`, `CircuitOpen`, `BudgetExceeded`, `Challenged`,
  `UpstreamError` -- and five places in `scraper.py` enumerated the ones they
  expected. `Disallowed` and `CircuitOpen` were missing from four of them, so
  either one raised *after* a shop's first page walked out of `fetch_html`
  untouched, and `main()` wrote a 0-product row and never called `absorb()`:
  the pages already parsed, their hits, their prices and their sold-out rows
  all went with it, and the digest then printed a "watched but found nowhere"
  line the aliases had not earned. A breaker tripping on page 40 of
  vinnouveau's 118 cost all 40, and needs no robots.txt to happen -- three
  consecutive failures on a host is the whole trigger. robots is the other
  route and it is live: winenot and pangee both publish `Disallow: /*?order=`
  and `Disallow: /*&order=`, and winenot's own pager writes
  `?page=36&order=product.name.asc`. (The PrestaShop default does *not*
  disallow `/nouveaux-produits` -- checked on both shops, and vinnouveau
  serves no robots.txt at all. A guess about what a platform's robots.txt
  contains is not evidence; read the file.)
  Two asymmetries inside that are load-bearing. **Page one still re-raises,
  and re-raises itself**, because `main()` has a handler per exception and
  they write different words -- `robots.txt`, `blocked`, `circuit open`,
  `unreachable` -- and a challenge in particular has to keep saying "blocked",
  since it is a shop saying no. **`UpstreamError` on a later page keeps its
  own branch above the base-class one and sets no `truncated` flag**, because
  several platforms end a catalogue by 404ing the next paged URL: folding it
  in would mark every such shop permanently TRUNCATED and make the one cell
  that decides whether a row is comparable with the shop's real selection
  useless. Everything else that stops a walk early *is* truncation and says
  so. A supplementary reader -- the producer index, the new-arrivals strip --
  catches the base class with no page-one exception at all: the catalogue is
  already in hand, and nothing a supplement does may cost it.
- **vinnaturel** is Cave de Trinquetaille, PrestaShop 1.6, and it was the
  third shop judged from the wrong page -- its landing page is not its
  catalogue, and reading it gave 12 products and a clean `ok` row for weeks.
  Its own menu offers no "all wines" link, so the bottle range is three colour
  categories (`/31-vins-rouges-bouteilles-` alone states "Il y a 108 produits"
  and pages to `?p=6`, plus `/28` blancs and `/29` roses). Bag-in-box lives
  separately under `/24` and stays out on purpose: a 3L BIB is not a bottle
  and would enter the price pool as one.
- Catalogues are paged. Any new fetcher must walk pages, not just read the
  first one -- seeing only page one turns a real hit into a silent miss,
  which is the exact failure this project exists to avoid.
- A "next" link is only a next page if its *page number* differs. winenot's
  last page links itself as `?page=36&order=product.name.asc` -- the same page
  with the sort the site was already applying written out -- and comparing the
  two as strings made every catalogue fetch its final page twice. `_distinct`
  compares the paging parameter, treating an unnumbered URL as page one.
- `wine.html` is generated by `dashboard.py`. Never edit it directly, and
  never commit a rebuilt copy on a feature branch: main rebuilds it on the
  push after a merge, so a branch-side copy only produces a merge conflict
  on a file whose correct resolution is always "regenerate it". Two merges
  stopped on exactly that. No test reads the file from disk -- they render
  through `dashboard.render()` -- so a branch never needs it current.
- Three workflows push to `main` and two of them rebuild `wine.html`, so a
  race on that file is normal -- and it is generated, so the only correct
  resolution is to regenerate it. Probe run 32268240645 died on exactly that
  conflict during its retry rebase, *after* it had fetched and parsed, losing
  everything it had observed. `probe.yml` now regenerates and continues when
  `wine.html` is the only conflicted file, and fails loudly on any other;
  `dashboard.yml` never rebases at all -- it resets to the new head, rebuilds
  on top, and exits quietly if someone else already did.
- A workflow input never reaches a shell command line, and neither does a
  `github.*` context. `tests/test_workflows.py` scanned only
  `inputs|github.event|steps`, so it could not see `probe.yml` splicing
  `${{ github.ref_name }}` into `git pull --rebase` in a job holding
  `contents: write` -- and a git ref may legally contain `$`, backticks,
  parentheses and semicolons, while `${{ }}` substitutes before the shell
  parses. It goes through
  `env:` and is read as a quoted variable -- `"$ONLY"`, never
  `--only ${{ inputs.only }}`. A probe dispatched with "Lapangee,
  lavinoterie" in that box became two shell arguments and crashed the run,
  and on a public repo the same shape is an injection waiting for a quote.
  The same goes for `github.event.*` and any `steps.*.outputs.*` derived
  from them. `tests/test_workflows.py` enforces this, plus two neighbours:
  every expansion is quoted, and every variable a script reads is defined
  on *that* step (an `env:` block one step away expands to the empty
  string, which is how `--kind ""` would have shipped).
- The probe saves what it finds by default. A read-only probe that reaches
  two shops, parses their catalogues and commits nothing looks identical to
  a probe that failed -- it kept lavinoterie and pangee dark for three days
  after they had already answered. `apply` defaults to true in `probe.yml`
  and in the dashboard form, and a read-only run that found working shops
  prints a `NOTHING WAS SAVED` block naming them and the next step. Safety
  is not lost: `--apply` still only promotes shops that parsed a real
  response in that same run, and only after the suite passes.
- `probe.py --only` is forgiving about how names are typed (commas or
  spaces, any case) and unforgiving about names that do not exist: an
  unknown name exits non-zero and lists the real ones. Probing the empty
  set and exiting 0 is the failure mode this replaces.
- The probe's catalogue-path guessing is capped (`MAX_CATALOGUE_GUESSES`).
  One live run spent 24 requests on a single shop, all 404, and 107 of 150
  across eight -- with eleven unverified shops the budget binds and the
  tail goes unprobed without saying so.
- The repo is public, so issue forms are untrusted input. `apply_issue.py`
  must keep rejecting quote/backslash injection, non-https URLs and unsafe
  shop names, and `apply-config.yml` must keep gating on the author's *real*
  repository permission, checked as the job's first step against
  `getCollaboratorPermissionLevel` and admitting `admin`/`maintain` only.
  It must never go back to `author_association`, which is a profile
  *visibility* rather than a permission: this repo is owned by an
  organisation, and an org member whose membership is private is reported
  `CONTRIBUTOR` or `NONE`, so the old `OWNER`-or-`MEMBER` test locked out an
  admin. Every run of that workflow concluded `skipped` from the move
  onwards -- ten runs, five correctly filled forms, including the
  mesbourgognes shop -- and a skipped job posts no comment and shows no red
  tick, so none of it was visible from the issue or the dashboard. A refusal
  now says so on the issue; plain `write` stays excluded, because push
  access to help is not authority to drive an unreviewed commit to main.
  Never put a token in `wine.html` -- it is world-readable.
  The page's own credential is read from `localStorage` at runtime and is
  never written into the generated file; `tests/test_dashboard.py` asserts
  that, and that the script only ever sends it to `api.github.com`.
- `wine.html` builds issue-form bodies in JavaScript that `apply_issue.py`
  parses in Python. Changing a form heading, its field order, or a checkbox
  label breaks that seam silently -- change `.github/ISSUE_TEMPLATE/`,
  `apply_issue.py` and `dashboard.JS` together. The node-backed tests in
  `tests/test_dashboard.py` run the real JS through the real parser; keep
  them working rather than reimplementing the format in the test.
- `apply-config.yml` commits to `main` without a PR, so its test step is
  the only thing standing between a bad edit and the default branch. Never
  reorder the commit ahead of the tests.
- Don't write tests that pin today's config state as an invariant ("every
  producer is unverified", "producer X does not exist"). The issue forms
  and `probe.py --apply` change that state, and such a test turns the next
  legitimate edit into a CI failure. Assert the rule, not the current data.
- No test may touch the network. `scraper.main()` fetches for real once any
  shop is verified, so tests that call it must stub `crawler.Crawler` --
  otherwise the suite hits live shops on every CI run.

## Subagents

- **shop-adapter** — add or repair per-shop scraping logic. Detects
  platform (Shopify `/products.json` → WooCommerce Store API → HTML
  fallback), requires a fixture per shop, keeps alias-aware producer
  matching, one shop per commit. Fetches through `Crawler`, never
  `requests` directly.
- **scrape-doctor** — diagnoses a zero-hit or wrong-hit run. Distinguishes
  unreachable vs. empty/JS-rendered vs. genuinely absent vs. parsed wrong,
  reproduces against the fixture before live network, and treats zero hits
  as a potentially correct result rather than something to "fix".
- **actions-ops** — runs/schedules/observes the GitHub Actions workflow.
  Owns the `DRY_RUN` path, never echoes secret values, and validates YAML
  after any workflow edit.
