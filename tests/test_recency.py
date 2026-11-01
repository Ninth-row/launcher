"""How soon the run can tell that a bottle is new.

Three separate things, all aimed at the same gap between a shop listing a
bottle and the email arriving:

- Shopify states `published_at` per product in the payload the run already
  fetches, and it was being discarded. For an allocated grower the difference
  between "listed four minutes ago" and "listed last month" is the whole
  decision.
- The new-arrivals strip is read *before* the catalogue. It used to be read
  after, which made the freshest page in the system the first thing a binding
  budget or clock took away.
- Inside a classification section the freshest listing leads, because that is
  the one with stock left.
"""
from datetime import datetime, timedelta, timezone

import crawler
import notify
import scraper

from canned_shop import product, shopify


def _grid(ids, next_url=None):
    cards = "".join(
        f'<div class="card"><a href="/wine-{n}">Ganevat cuvee {n}</a>'
        f'<span>{20 + n},00 &euro;</span></div>' for n in ids)
    nxt = f'<a rel="next" href="{next_url}">next</a>' if next_url else ""
    return f'<html><body><div class="grid">{cards}</div>{nxt}</body></html>'


SHOP = {"name": "zzzshop", "platform": "html", "url": "https://shop.test",
        "item_selector": "div.product", "title_selector": "h2.product-title",
        "price_selector": "span.price", "verified": True}


class Stub:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []
        self.max_requests = 1000
        self.request_count = 0

    def get(self, url, params=None, max_age=None):
        self.request_count += 1
        full = url
        if params:
            full += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self.requested.append(full)
        return crawler.FetchResult(200, self.pages.get(full, ""))

    def reopen(self, url):
        pass


# --- the strip goes first ---------------------------------------------------

def test_the_new_arrivals_strip_is_fetched_before_the_catalogue():
    """The order is the whole point: a catalogue cut short is a TRUNCATED row
    that says so, a strip never read is a bottle nobody hears about."""
    shop = dict(SHOP, new_arrivals="nouveaux-produits")
    client = Stub({"https://shop.test": _grid([1, 2, 3]),
                   "https://shop.test/nouveaux-produits": _grid([7, 8, 9])})

    scraper.fetch_html(shop, client)

    assert client.requested[0] == "https://shop.test/nouveaux-produits", \
        f"the catalogue was read first: {client.requested}"


def test_the_strip_is_read_fresh_and_the_catalogue_is_not():
    """The strip opts out of the 6h cache; a catalogue page does not."""
    shop = dict(SHOP, new_arrivals="nouveaux-produits")
    seen = []

    class Recording(Stub):
        def get(self, url, params=None, max_age=None):
            seen.append((url, max_age))
            return super().get(url, params, max_age)

    client = Recording({"https://shop.test": _grid([1, 2, 3]),
                        "https://shop.test/nouveaux-produits": _grid([7, 8, 9])})
    scraper.fetch_html(shop, client)

    ages = dict(seen)
    assert ages["https://shop.test/nouveaux-produits"] == \
        crawler.FRESH_PAGE_TTL_SECONDS
    assert ages["https://shop.test"] is None


# --- and off_catalogue still means what it says ----------------------------

def test_off_catalogue_counts_only_what_the_catalogue_lacks():
    shop = dict(SHOP, new_arrivals="nouveaux-produits")
    client = Stub({"https://shop.test": _grid([1, 2, 3]),
                   "https://shop.test/nouveaux-produits": _grid([3, 4, 5])})

    items = scraper.fetch_html(shop, client)

    # wine-3 is in both; wine-4 and wine-5 are off-catalogue.
    assert items.off_catalogue == 2
    assert len(items) == 5


def test_a_strip_the_catalogue_already_holds_reads_zero():
    """The healthy case, and the one the ordering change could have broken:
    merging the strip before the catalogue would have counted every listing
    as off-catalogue."""
    shop = dict(SHOP, new_arrivals="nouveaux-produits")
    client = Stub({"https://shop.test": _grid([1, 2, 3]),
                   "https://shop.test/nouveaux-produits": _grid([1, 2, 3])})

    items = scraper.fetch_html(shop, client)

    assert items.off_catalogue == 0
    assert len(items) == 3


# --- Shopify states its own release time -----------------------------------

def test_shopify_carries_the_publication_time_into_the_hit():
    body = shopify([dict(product("Zzz Domaine Cuvee 2020", 40),
                         published_at="2026-07-24T15:24:03+02:00")])
    client = Stub({"https://shopify.test/products.json?limit=250&page=1": body})

    items = scraper.fetch_shopify(
        {"name": "zzz", "url": "https://shopify.test"}, client)

    assert items[0]["listed_at"] == "2026-07-24T15:24:03+02:00"


def test_created_at_is_only_the_fallback():
    """published_at is when it went on sale; created_at is when the record was
    made, and they can be months apart."""
    body = shopify([dict(product("Zzz Domaine Cuvee 2020", 40),
                         created_at="2026-05-20T12:02:16+02:00")])
    client = Stub({"https://shopify.test/products.json?limit=250&page=1": body})

    items = scraper.fetch_shopify(
        {"name": "zzz", "url": "https://shopify.test"}, client)

    assert items[0]["listed_at"] == "2026-05-20T12:02:16+02:00"


def test_an_html_shop_states_no_listing_time():
    client = Stub({"https://shop.test": _grid([1, 2, 3])})
    items = scraper.fetch_html(SHOP, client)
    assert all(i.get("listed_at") is None for i in items)


# --- how it reads in the email ---------------------------------------------

def _now():
    return datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def test_the_age_is_phrased_by_distance():
    def age(**kw):
        return notify._listed_age(
            {"listed_at": (_now() - timedelta(**kw)).isoformat()}, _now())

    assert age(minutes=4) == "4 min ago"
    assert age(minutes=75) == "75 min ago"
    assert age(hours=5) == "5h ago"
    assert age(days=5) == "5 days ago"


def test_a_malformed_or_naive_timestamp_cannot_break_the_digest():
    assert notify._listed_age({"listed_at": "not a date"}, _now()) is None
    assert notify._listed_age({"listed_at": "2026-11-01T10:00:00"}, _now()) is None
    assert notify._listed_age({}, _now()) is None
    # A shop whose clock runs ahead of ours tells us nothing useful.
    assert notify._listed_age(
        {"listed_at": "2027-01-01T00:00:00+00:00"}, _now()) is None


def test_a_new_listing_says_how_old_it_is():
    hit = {"alert_reason": "new",
           "listed_at": (datetime.now(timezone.utc)
                         - timedelta(minutes=12)).isoformat()}
    assert notify.reason_phrase(hit).startswith("new listing, 1")

    assert notify.reason_phrase({"alert_reason": "new"}) == "new listing"


def test_the_freshest_listing_leads_its_section():
    old = {"classification": "DEAL", "title": "older",
           "listed_at": "2026-10-01T00:00:00+00:00"}
    new = {"classification": "DEAL", "title": "newer",
           "listed_at": "2026-10-31T00:00:00+00:00"}
    undated = {"classification": "DEAL", "title": "undated"}

    ordered = notify.order_hits([old, undated, new])

    assert [h["title"] for h in ordered] == ["newer", "older", "undated"], \
        "the undated HTML shops must not be guessed to either end of time"


def test_classification_still_outranks_freshness():
    """A DEAL listed yesterday beats a FAIR listed a minute ago -- the section
    decides first, and only then the clock."""
    fair_new = {"classification": "FAIR", "title": "fair",
                "listed_at": "2026-10-31T00:00:00+00:00"}
    deal_old = {"classification": "DEAL", "title": "deal",
                "listed_at": "2026-01-01T00:00:00+00:00"}

    assert [h["title"] for h in notify.order_hits([fair_new, deal_old])] == \
        ["deal", "fair"]
