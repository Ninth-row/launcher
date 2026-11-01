"""The bottles we have already found, remembered by URL.

A catalogue walk discovers a *new* bottle, and it is the first thing the
request budget and the run clock cut. A TRUNCATED row means some of a shop's
pages went unread -- and the few dozen URLs we actually care about were on
them last time. Nothing persisted those URLs before this: observations.json
keeps (producer, seg, shop) and no URL at all, seen.json keeps only a sha256
of one, and hits.json is not carried between runs. So a watched bottle went
unchecked whenever its shop's walk was cut short, and nothing said so.
"""
import datetime as dt
import json

import crawler
import scraper


def _product_page(price, quantity):
    """A PrestaShop product page, which states both on its add-to-cart form."""
    island = json.dumps({"price_amount": price, "quantity": quantity})
    return f'<html><body><div data-product=\'{island}\'></div></body></html>'


class Pages:
    def __init__(self, pages, raises=None):
        self.pages = pages
        self.raises = dict(raises or {})
        self.requested = []
        self.max_requests = 1000
        self.request_count = 0

    def get(self, url, params=None, max_age=None):
        self.request_count += 1
        self.requested.append(url)
        for fragment, exc in self.raises.items():
            if fragment in url:
                raise exc
        return crawler.FetchResult(200, self.pages.get(url, ""))


SHOP = {"name": "zzzshop", "url": "https://shop.test"}


# --- remembering and forgetting -------------------------------------------

def test_a_hit_is_remembered_by_its_url():
    store = scraper.merge_watchlist(
        {}, [{"url": "https://shop.test/p/1", "shop": "zzzshop",
              "producer": "Ganevat", "title": "Ganevat 2020"}],
        today=dt.date(2026, 11, 1))

    assert store["https://shop.test/p/1"] == {
        "shop": "zzzshop", "producer": "Ganevat",
        "title": "Ganevat 2020", "last_confirmed": "2026-11-01"}


def test_an_unconfirmed_url_is_forgotten_after_the_observation_window():
    today = dt.date(2026, 11, 1)
    old = (today - dt.timedelta(days=scraper.WATCHLIST_MAX_AGE_DAYS + 1)).isoformat()
    fresh = (today - dt.timedelta(days=5)).isoformat()
    store = {
        "https://shop.test/gone": {"shop": "zzzshop", "last_confirmed": old},
        "https://shop.test/here": {"shop": "zzzshop", "last_confirmed": fresh},
    }

    kept = scraper.merge_watchlist(store, [], today=today)

    assert "https://shop.test/here" in kept
    assert "https://shop.test/gone" not in kept, \
        "past the window a URL is likelier to 404 than to sell anything"


def test_an_entry_with_no_usable_date_is_dropped_rather_than_kept_forever():
    kept = scraper.merge_watchlist(
        {"https://shop.test/x": {"shop": "zzzshop"},
         "https://shop.test/y": {"shop": "zzzshop", "last_confirmed": "soon"}},
        [], today=dt.date(2026, 11, 1))
    assert kept == {}


def test_a_lost_watchlist_reads_as_empty_not_as_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "WATCHLIST_PATH", tmp_path / "missing.json")
    assert scraper.load_watchlist() == {}

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(scraper, "WATCHLIST_PATH", bad)
    assert scraper.load_watchlist() == {}

    wrong = tmp_path / "wrong.json"
    wrong.write_text('["a list, not a map"]')
    monkeypatch.setattr(scraper, "WATCHLIST_PATH", wrong)
    assert scraper.load_watchlist() == {}


# --- re-reading what the walk missed --------------------------------------

def test_a_watched_url_is_re_read_from_its_product_page():
    entries = [("https://shop.test/p/1",
                {"title": "Ganevat Chalasses 2020", "shop": "zzzshop"})]
    client = Pages({"https://shop.test/p/1": _product_page(91.0, 3)})

    hits, sold_out = scraper.recheck_watchlist(SHOP, entries, client)

    assert sold_out == []
    assert len(hits) == 1
    assert hits[0]["price"] == 91.0
    assert hits[0]["producer"] == "Ganevat"
    assert hits[0]["url"] == "https://shop.test/p/1"


def test_a_sold_out_re_read_is_not_a_find():
    """PrestaShop states `quantity` on the product page. A listing nobody can
    buy must stay out of hits -- out of hits.json, out of the price pool and
    above all out of seen.json, which is what lets its restock read as new."""
    entries = [("https://shop.test/p/1",
                {"title": "Ganevat Chalasses 2020", "shop": "zzzshop"})]
    client = Pages({"https://shop.test/p/1": _product_page(91.0, 0)})

    hits, sold_out = scraper.recheck_watchlist(SHOP, entries, client)

    assert hits == []
    assert len(sold_out) == 1
    assert sold_out[0]["producer"] == "Ganevat"


def test_the_producer_is_re_derived_and_not_trusted_from_the_entry():
    """Attribution comes from matching and nowhere else. A remembered producer
    would outlive an alias being corrected, which is the bug NAMESAKES exists
    to prevent."""
    entries = [("https://shop.test/p/1",
                {"title": "Clos-Vougeot Grand Cru ROUMIER LAURENT 2021",
                 "producer": "Ganevat", "shop": "zzzshop"})]
    client = Pages({"https://shop.test/p/1": _product_page(91.0, 3)})

    hits, sold_out = scraper.recheck_watchlist(SHOP, entries, client)

    assert hits == [] and sold_out == [], \
        "a stored producer was trusted over what the title actually says"


def test_a_host_saying_no_this_run_stops_the_whole_re_read():
    entries = [(f"https://shop.test/p/{n}",
                {"title": "Ganevat Chalasses 2020"}) for n in (1, 2, 3)]
    client = Pages({}, raises={"p/1": crawler.Disallowed("https://shop.test/p/1")})

    hits, _ = scraper.recheck_watchlist(SHOP, entries, client)

    assert hits == []
    assert len(client.requested) == 1, \
        "robots refused one page and the rest were asked anyway"


def test_one_dead_page_does_not_stop_the_others():
    entries = [("https://shop.test/p/1", {"title": "Ganevat A 2020"}),
               ("https://shop.test/p/2", {"title": "Ganevat B 2020"})]
    client = Pages(
        {"https://shop.test/p/2": _product_page(91.0, 3)},
        raises={"p/1": crawler.UpstreamError("HTTP 404", status_code=404)})

    hits, _ = scraper.recheck_watchlist(SHOP, entries, client)

    assert len(hits) == 1 and hits[0]["url"] == "https://shop.test/p/2"


def test_the_re_read_is_capped():
    entries = [(f"https://shop.test/p/{n}", {"title": "Ganevat 2020"})
               for n in range(scraper.MAX_WATCHLIST_FETCHES + 10)]
    client = Pages({})

    scraper.recheck_watchlist(SHOP, entries, client)

    assert len(client.requested) == scraper.MAX_WATCHLIST_FETCHES


# --- through a whole run ---------------------------------------------------

def test_a_full_pass_remembers_what_it_found(pipeline):
    from canned_shop import product, shopify
    pipeline({"https://shopify.test": shopify(
        [product("Zzz Domaine Cuvee 2020", 40)])}, force=True)

    store = json.loads((pipeline.tmp / "watchlist.json").read_text())
    assert any(e["producer"] == "Zzz Domaine" for e in store.values())


def test_a_hot_pass_neither_reads_nor_writes_the_watchlist(pipeline):
    """A hot pass reads one surface per shop, so the URLs it did not see are
    not absent -- they were never looked for, and pruning on that basis would
    forget the whole list."""
    from canned_shop import product, shopify
    bodies = {"https://shopify.test": shopify(
        [product("Zzz Domaine Cuvee 2020", 40)])}

    pipeline(bodies, hot=True, force=True)

    assert not (pipeline.tmp / "watchlist.json").exists()
