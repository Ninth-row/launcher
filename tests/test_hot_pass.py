"""The hot pass: read the one page a release lands on, and nothing else.

A full pass is ~410 requests and 11-32 minutes against a 450 budget, and every
catalogue page it reads comes from a 6h cache -- so it cannot run often enough,
or freshly enough, to win a race for a bottle that exists in dozens. A hot pass
is ~24 requests and under two minutes.

The point of this file is that a hot pass must not *pretend* to be a full one.
It reads one surface per shop, so almost every conclusion main() draws from a
complete crawl is unavailable to it, and the dangerous ones are not the noisy
notes -- it is the state. notify._update_state stamps `last_seen_at` for every
in-stock hit whether it alerted or not, and _came_back refuses to call a bottle
"back" when that stamp is newer than ABSENT_DAYS. A hot pass that merely looked
at a known bottle would therefore restamp its presence and silently destroy the
restock alert. The first test here is that one.
"""
import json
from datetime import datetime, timedelta, timezone

import notify
import scraper

from canned_shop import FakeCrawler, SHOPS, html_listing, product, shopify, woo, woo_product

BASIC_HTML = {
    "https://shopify.test": shopify([]),
    "https://woo.test": woo([]),
    "https://html.test": html_listing([
        ("Zzz Other Savagnin 2019", 40, False),
        ("Zzz Other Trousseau 2021", 35, False),
        ("Zzz Other Ploussard 2020", 30, False),
    ]),
}

BODIES = {
    "https://shopify.test": shopify([product("Zzz Domaine Cuvee 2020", 40)]),
    "https://woo.test": woo([woo_product("Zzz Negoce Rouge 2021", 30)]),
}


def _state(pipeline):
    return json.loads((pipeline.tmp / "seen.json").read_text())


# --- the one that would lose a real find -----------------------------------

def test_a_hot_pass_does_not_restamp_a_bottle_it_merely_looked_at(pipeline):
    """The restock alert survives a hot pass.

    A bottle last seen 10 days ago is back on the shelf. The hot pass sees it,
    must not alert (it is not a new URL), and above all must not refresh its
    `last_seen_at` -- because that is what _came_back measures, and refreshing
    it leaves the bottle not new, not a 10% drop, not back, and inside the
    30-day cooldown: silent for a month.
    """
    pipeline(BODIES, force=True)                      # a full pass, to seed
    before = _state(pipeline)
    keys = [k for k in before if k != notify.META_KEY]
    assert keys, "the seeding run recorded nothing"

    long_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    for key in keys:
        before[key]["last_seen_at"] = long_ago
    (pipeline.tmp / "seen.json").write_text(json.dumps(before))

    pipeline(BODIES, hot=True)

    after = _state(pipeline)
    for key in keys:
        assert after[key]["last_seen_at"] == long_ago, \
            "the hot pass restamped presence and silenced the restock"


def test_a_hot_pass_does_not_claim_to_have_read_the_shop(pipeline):
    """_meta.shops is the "were we actually looking" guard behind _came_back.
    Reading a new-arrivals strip is not reading the shop."""
    pipeline(BODIES, hot=True, force=True)

    meta = _state(pipeline).get(notify.META_KEY, {})
    assert not meta.get("shops"), \
        "a strip read was recorded as a successful read of the whole shop"


# --- what it does alert on -------------------------------------------------

def test_a_never_seen_listing_is_emailed(pipeline):
    pipeline(BODIES, hot=True)
    body = "\n".join(pipeline.sent)
    assert "Zzz Domaine" in body, "a brand-new listing did not reach the inbox"


def test_a_listing_already_known_sends_nothing(pipeline, capsys):
    pipeline(BODIES, force=True)        # seed it
    pipeline.sent.clear()
    capsys.readouterr()

    pipeline(BODIES, hot=True)

    assert pipeline.sent == [], "a hot pass emailed a bottle it already knew"
    assert "silent run is valid" in capsys.readouterr().out


def test_a_hot_pass_reads_only_shops_with_a_recency_surface(pipeline):
    """zzz-shopify states published_at per product; zzz-woo and zzz-html have
    no configured new-arrivals path, so they are not read at all rather than
    read badly."""
    client = pipeline(BODIES, hot=True)

    assert any("shopify.test" in u for u in client.urls)
    assert not any("woo.test" in u for u in client.urls), \
        "a shop with no recency surface was crawled on a hot pass"


# --- what it must not write or say ----------------------------------------

def test_a_hot_pass_writes_neither_coverage_nor_the_price_pool(pipeline):
    pipeline(BODIES, hot=True, force=True)

    assert not (pipeline.tmp / "coverage.json").exists(), \
        "a one-page read overwrote the table describing every catalogue"
    assert not (pipeline.tmp / "observations.json").exists(), \
        "a biased slice of one shop was written into the reference pool"


def test_a_hot_pass_accuses_neither_the_adapters_nor_the_watchlist(
        pipeline, capsys):
    """DRIFT, "found nowhere" and near-misses are all conclusions about a
    complete crawl. Printed hourly off one page each they would be an
    accusation every hour, and the word stops being read."""
    pipeline(BODIES, hot=True, force=True)
    out = capsys.readouterr().out

    assert "DRIFT" not in out
    assert "found nowhere" not in out
    assert "near-miss" not in out.lower()
    assert "sold out everywhere" not in out


def test_the_full_pass_still_says_all_of_it(pipeline, capsys):
    """The guards are conditional, not deletions -- a normal run is unchanged.

    zzz-html is served no body here, so its honest verdict is "empty
    response" rather than DRIFT; what matters is that the full pass still
    reaches a verdict and still names the producers nobody stocks, where the
    hot pass above says neither.
    """
    pipeline(BODIES, force=True)
    out = capsys.readouterr().out

    assert "empty response" in out
    assert "found nowhere" in out


def test_the_landing_page_counts_as_a_recency_surface():
    """An empty new_arrivals path means the shop's own landing page.

    leszinzinsduvin puts its newest arrivals there -- its cards are literally
    `novelties__card` -- and has nothing else crawlable, so read as falsy that
    shop gets no hourly cover at all. Three of Alice Fahrenkrug's 2025s were
    listed and sold out inside one afternoon, between two full passes, with
    only hot passes in between and this shop in none of them.
    """
    assert scraper.hot_surface({"platform": "html", "new_arrivals": ""})
    assert not scraper.hot_surface({"platform": "html"})
    assert scraper._shop_url({"url": "https://x.test"}, "") == "https://x.test/"


# --- the guarantee, as a build failure --------------------------------------

def test_every_verified_shop_is_swept_or_says_why_not():
    """The test that encodes "this must not happen at any shop".

    Alice Fahrenkrug's three 2025s were listed and sold out inside about seven
    hours at a shop that had no recency surface, and nothing anywhere said so:
    `hot_surface()` quietly returned False and that shop was dropped from
    every sweep. Seven of eighteen shops were in that state.

    So a shop may have no surface only by declaring a measured reason. This
    asserts the *rule*, never today's config -- a shop added tomorrow with
    neither a surface nor a reason fails the build, which is the only form a
    guarantee like this can actually take.
    """
    undeclared = [s["name"] for s in scraper.SHOPS
                  if s.get("verified", True)
                  and scraper.recency_gap(s) == "undeclared"]
    assert not undeclared, (
        f"these verified shops are read only by the 2-hourly full pass and do "
        f"not say why: {', '.join(undeclared)}. Give each a recency surface, "
        f"or a `no_recency_signal` naming the measured reason it has none."
    )


def test_a_declared_reason_is_accepted_but_a_blank_one_is_not():
    """`no_recency_signal: ""` would pass a truthiness check and say nothing."""
    assert scraper.recency_gap(
        {"platform": "html", "no_recency_signal": "catalogue is behind a login"}
    ) == "catalogue is behind a login"
    assert scraper.recency_gap({"platform": "html"}) == "undeclared"
    assert scraper.recency_gap({"platform": "html", "no_recency_signal": ""}) \
        == "undeclared"
    assert scraper.recency_gap({"platform": "shopify"}) is None


def test_every_surface_kind_states_what_it_costs():
    """The budget is sized from these numbers, so an unpriced kind is a
    sweep that silently overruns and truncates."""
    for shop in scraper.SHOPS:
        if not shop.get("verified", True) or not scraper.hot_surface(shop):
            continue
        assert scraper.hot_sweep_pages(shop) > 0, shop["name"]


# --- a sweep that reads whole catalogues -------------------------------------
#
# Five shops are small enough that a sweep reads their entire catalogue, so a
# sweep now looks at a great many bottles it already knows. The guard that
# makes that safe -- only URLs absent from seen.json ever reach the digest --
# stops being a nicety and becomes the thing standing between a 15-minute
# cadence and the destruction of every restock alert.

def _catalogue_sweep_shops():
    return [
        {**s, "hot": "catalogue", "hot_pages": 1} if s["name"] == "zzz-html" else s
        for s in SHOPS
    ]


def test_a_catalogue_sweep_leaves_a_known_bottle_untouched(pipeline, monkeypatch):
    """The blocking test. A sweep reading a whole catalogue sees every bottle
    on it; not one of the known ones may be restamped, or `_came_back` is
    destroyed for the whole shop and the silence lasts a month."""
    monkeypatch.setattr(scraper, "SHOPS", _catalogue_sweep_shops())
    pipeline(BASIC_HTML, force=True)                  # seed from a full pass
    before = _state(pipeline)
    keys = [k for k in before if k != notify.META_KEY]
    assert keys, "the seeding run recorded nothing"

    long_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    for key in keys:
        before[key]["last_seen_at"] = long_ago
    (pipeline.tmp / "seen.json").write_text(json.dumps(before))
    seeded = (pipeline.tmp / "seen.json").read_text()

    pipeline(BASIC_HTML, hot=True)

    assert (pipeline.tmp / "seen.json").read_text() == seeded, \
        "a catalogue sweep rewrote the file that decides what has been alerted"


def test_a_catalogue_sweep_still_alerts_on_something_new(pipeline, monkeypatch):
    monkeypatch.setattr(scraper, "SHOPS", _catalogue_sweep_shops())
    pipeline(BASIC_HTML, force=True)
    sent_before = len(pipeline.sent)

    grown = dict(BASIC_HTML)
    grown["https://html.test"] = html_listing([
        ("Zzz Other Savagnin 2019", 40, False),
        ("Zzz Other Trousseau 2021", 35, False),
        ("Zzz Other Ploussard 2020", 30, False),
        ("Zzz Domaine Melon 2024", 55, False),
    ])
    pipeline(grown, hot=True)

    assert len(pipeline.sent) == sent_before + 1
    assert "Melon" in pipeline.sent[-1]


def test_every_page_of_a_sweep_is_read_fresh(monkeypatch):
    """Not just page one.

    A walk that passes `max_age` to its first request and lets the rest fall
    back to the 6h catalogue cache reports a fresh read and performs a stale
    one. That is exactly how a shop can be swept every fifteen minutes and
    still serve a page written before the release -- the leszinzinsduvin
    lesson one level down, and invisible from any log.
    """
    shop = {**next(s for s in SHOPS if s["name"] == "zzz-html"),
            "hot": "catalogue", "hot_pages": 3}
    pages = {
        "https://html.test": html_listing([
            ("Zzz Other Savagnin 2019", 40, False),
            ("Zzz Other Trousseau 2021", 35, False),
            ("Zzz Other Ploussard 2020", 30, False),
        ]).replace("</body>", '<a rel="next" href="/p2">next</a></body>'),
        "https://html.test/p2": html_listing([
            ("Zzz Other Chardonnay 2022", 42, False),
            ("Zzz Other Poulsard 2023", 38, False),
            ("Zzz Other Melon 2021", 31, False),
        ]),
    }
    client = FakeCrawler(pages)
    scraper.fetch_hot(shop, client, totals={})

    walked = [(u, a) for u, a in client.max_ages if u.startswith("https://html.test")]
    assert len(walked) >= 2, f"the walk did not reach a second page: {walked}"
    stale = [u for u, age in walked if age != scraper.SWEEP_PAGE_TTL_SECONDS]
    assert not stale, f"these pages were allowed to come from the 6h cache: {stale}"


# --- does the surface we sweep actually carry what we hunt? ------------------

def test_a_full_pass_measures_what_the_sweep_surface_would_have_missed(
        pipeline, monkeypatch):
    """The residual, as a number rather than an assumption.

    A shop swept through a strip is covered only to the extent the strip
    carries the bottles we watch. winenot's catalogue is a colour-facet
    union, so PACK OVERNOY SAVAGNIN 2018 sat in stock at EUR 580 and
    unreported because a pack carries no colour. Nothing measured that.
    """
    shops = [
        {**s, "new_arrivals": "new"} if s["name"] == "zzz-html" else s
        for s in SHOPS
    ]
    monkeypatch.setattr(scraper, "SHOPS", shops)
    # The strip carries the catalogue's first bottle (same URL) and two
    # others that are not watched. So of the three hits the catalogue yields,
    # a sweep of the strip would have seen exactly one.
    strip = (
        '<html><body><div class="grid">'
        '<div><a href="/p/0">Zzz Other Savagnin 2019</a><span>40,00 &euro;</span></div>'
        '<div><a href="/n/1">Something Else 2020</a><span>12,00 &euro;</span></div>'
        '<div><a href="/n/2">Another Thing 2021</a><span>14,00 &euro;</span></div>'
        '</div></body></html>'
    )
    bodies = {
        "https://html.test/new": strip,
        "https://html.test": html_listing([
            ("Zzz Other Savagnin 2019", 40, False),
            ("Zzz Other Trousseau 2021", 35, False),
            ("Zzz Other Ploussard 2020", 30, False),
        ]),
        "https://shopify.test": shopify([]),
        "https://woo.test": woo([]),
    }
    pipeline(bodies, force=True)

    body = pipeline.sent[-1]
    assert "sweep surface carries" in body, \
        "the run did not say how much of what it found a sweep would have seen"


def test_a_shop_swept_whole_reports_no_shortfall(pipeline, monkeypatch):
    """Where the sweep reads the whole catalogue the question is moot, and a
    note that fires on every shop every run is a note nobody reads."""
    monkeypatch.setattr(scraper, "SHOPS", _catalogue_sweep_shops())
    pipeline(BASIC_HTML, force=True)
    assert "sweep surface carries" not in pipeline.sent[-1]


def test_a_sweep_records_that_it_ran_and_the_full_pass_says_so(pipeline):
    """A quiet market and a dead trigger send the same number of emails.

    So the sweep stamps when it last looked, and the full pass renders the
    elapsed time into the digest. Without the stamp the note silently reads
    "no sweep recorded yet" for ever, which is worse than saying nothing
    because it accuses a trigger that is working.
    """
    pipeline(BODIES, hot=True, force=True)

    totals = json.loads((pipeline.tmp / "totals.json").read_text())
    assert totals.get(scraper.TOTALS_META_KEY, {}).get("last_sweep_at"), \
        "the sweep did not record that it ran"

    pipeline(BODIES, force=True)
    assert "last sweep" in pipeline.sent[-1].lower()


def test_a_dry_run_records_nothing(pipeline):
    pipeline(BODIES, hot=True, force=True, dry_run=True)
    assert not (pipeline.tmp / "totals.json").exists()


# --- a surface that does not work must not read as cover ---------------------
#
# The declaration test asserts a shop *has* a surface, never that the surface
# returns anything. The first live 18-shop sweep found both ways that gap
# bites: mesbourgognes' configured new-arrivals path answered HTTP 404 and the
# row said `unreachable`, and vinnouveau's strip read 1 page and 0 products
# under a clean `ok`. Both shops counted as covered. That is the Fahrenkrug
# failure in a new coat.

def test_a_404_is_a_path_to_correct_not_an_outage(pipeline, capsys):
    """Waiting does not fix a 404.

    `unreachable` reads as a network fault someone could wait out, so a shop
    whose configured path has moved sits in the list looking like an outage
    while covering nothing. NOT_THERE_STATUSES already said a 404 is "just as
    settled" as a refusal; only the reporting disagreed.
    """
    pipeline(BODIES, hot=True, force=True, refuse_hosts={"shopify.test": 404})

    out = capsys.readouterr().out
    assert "path is not there" in out
    assert "unreachable" not in out
    body = pipeline.sent[-1]
    assert "Configured path is not there" in body
    assert "zzz-shopify" in body


def test_a_surface_that_carries_nothing_is_named(pipeline, capsys):
    """A strip lists the newest N products whether or not any are new, so
    parsing zero from one is a path or an adapter problem, never a quiet
    market -- and a quiet market is exactly what it used to look like."""
    bodies = dict(BODIES)
    bodies["https://shopify.test"] = shopify([])
    pipeline(bodies, hot=True, force=True)

    body = pipeline.sent[-1]
    assert "surface carried no products" in body
    assert "zzz-shopify" in body
    assert "has never carried anything" in body


def test_a_surface_that_worked_before_says_when(pipeline):
    """One empty read is weak evidence; a week of them is conclusive. So the
    note carries the date the surface last carried anything."""
    pipeline(BODIES, hot=True, force=True)          # a productive sweep
    totals = json.loads((pipeline.tmp / "totals.json").read_text())
    assert totals[scraper.TOTALS_META_KEY]["surface_ok"].get("zzz-shopify")

    bodies = dict(BODIES)
    bodies["https://shopify.test"] = shopify([])
    pipeline(bodies, hot=True, force=True)

    body = pipeline.sent[-1]
    assert "last carried anything" in body
    assert "has never carried anything" not in body


def test_the_run_states_what_it_spent(pipeline, capsys):
    """The budget is what bounds coverage now. Read off the PAGES column it
    misses the robots.txt fetches, the documents and the redirect hops, so
    the run prints the number it actually has."""
    pipeline(BODIES, hot=True, force=True)
    assert "Requests spent:" in capsys.readouterr().out
    assert "Requests spent" in pipeline.sent[-1]
