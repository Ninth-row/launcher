"""A failed page is not a failed shop.

`Crawler.get()` raises five things and they all descend from
`crawler.FetchError`, but every handler in `scraper.py` named its subclasses
in a tuple instead -- five tuples, and `Disallowed` and `CircuitOpen` were
missing from four of them. Either one, raised anywhere after a shop's first
page, walked straight out of `fetch_html` and reached `main()`, which wrote a
0-product coverage row and skipped `absorb()` entirely: the pages already
parsed, the hits on them, their contribution to the price pool and their
sold-out rows all went with it, and the digest then printed a "watched but
found nowhere" line the aliases had not earned.

The breaker is the reachable one -- three consecutive failures on a host and
the next page of that same shop raises `CircuitOpen`, with no robots.txt
involved at all. robots is the other: winenot and pangee both publish
`Disallow: /*?order=` and `Disallow: /*&order=`, and winenot's pager writes
`?page=36&order=product.name.asc`.

The asymmetry these tests pin is the point. A first page that fails is still
the shop failing, and must still reach `main()` as *itself*, because there is
a handler per exception and they write different coverage words -- `robots.txt`,
`blocked`, `circuit open`. Only a later page is allowed to end the walk
quietly.
"""
import pytest

import crawler
import scraper


def grid(ids, next_url=None):
    cards = "".join(
        f'<div class="card"><a href="/wine-{n}">Ganevat cuvee {n}</a>'
        f'<span>{20 + n},00 &euro;</span></div>' for n in ids)
    nxt = f'<a rel="next" href="{next_url}">next</a>' if next_url else ""
    return f'<html><body><div class="grid">{cards}</div>{nxt}</body></html>'


# Selectors that deliberately match nothing, so every test here goes down
# the autoselect path the real HTML shops use.
SHOP = {"name": "zzzshop", "platform": "html", "url": "https://shop.test",
        "item_selector": "div.product", "title_selector": "h2.product-title",
        "price_selector": "span.price", "verified": True}


class Stub:
    """Serves a url -> body map and raises a chosen error for chosen URLs.

    Keyed on the URL *with its query string*, so a paged JSON endpoint that
    varies only by params can still be told apart page by page.
    """

    def __init__(self, pages, raises=None):
        self.pages = pages
        self.raises = dict(raises or {})
        self.requested = []
        self.max_requests = 1000
        self.request_count = 0

    def get(self, url, params=None, max_age=None):
        self.request_count += 1
        full = url
        if params:
            full += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self.requested.append(full)
        for fragment, exc in self.raises.items():
            if fragment in full:
                raise exc
        return crawler.FetchResult(200, self.pages.get(full, ""))

    def reopen(self, url):
        pass


# --- the breaker, which needs no robots.txt to bite -------------------------

def test_a_breaker_tripping_mid_walk_keeps_the_pages_already_read(capsys):
    client = Stub(
        {"https://shop.test": grid([1, 2, 3], "https://shop.test/?page=2"),
         "https://shop.test/?page=2": grid([4, 5, 6], "https://shop.test/?page=3")},
        raises={"page=3": crawler.CircuitOpen("shop.test")})

    items = scraper.fetch_html(SHOP, client)

    assert len(items) == 6, "the breaker discarded two pages already parsed"
    assert items.truncated, "a walk that stopped early must say TRUNCATED"
    assert "CircuitOpen" in capsys.readouterr().out


def test_a_breaker_open_on_page_one_is_still_the_shop_failing():
    # main() has a handler for this that writes "circuit open" and queues the
    # shop for the retry pass. Swallowing it here would lose both.
    client = Stub({}, raises={"shop.test": crawler.CircuitOpen("shop.test")})
    with pytest.raises(crawler.CircuitOpen):
        scraper.fetch_html(SHOP, client)


# --- robots -----------------------------------------------------------------

def test_a_disallowed_new_arrivals_path_does_not_discard_the_catalogue(capsys):
    """The headline case. The strip is supplementary -- it may cost us the
    strip, never the catalogue that was already read."""
    shop = dict(SHOP, new_arrivals="nouveaux-produits")
    client = Stub(
        {"https://shop.test": grid([1, 2, 3])},
        raises={"nouveaux-produits": crawler.Disallowed(
            "https://shop.test/nouveaux-produits")})

    items = scraper.fetch_html(shop, client)

    assert len(items) == 3, "a disallowed strip took the whole catalogue with it"
    assert items.off_catalogue == 0
    out = capsys.readouterr().out
    assert "could not be read" in out, "the strip failed and said nothing"
    assert "the catalogue alone was used" in out


def test_a_disallowed_producer_index_does_not_discard_the_catalogue():
    shop = dict(SHOP, producer_index="vignerons")
    client = Stub({"https://shop.test": grid([1, 2, 3])},
                  raises={"vignerons": crawler.Disallowed(
                      "https://shop.test/vignerons")})

    assert len(scraper.fetch_html(shop, client)) == 3


def test_one_disallowed_category_does_not_black_out_the_shop():
    shop = dict(SHOP, catalog_paths=["a", "b"])
    client = Stub({"https://shop.test/a": grid([1, 2, 3])},
                  raises={"shop.test/b": crawler.Disallowed("https://shop.test/b")})

    items = scraper.fetch_html(shop, client)

    assert len(items) == 3, "one disallowed category took the other with it"
    assert items.truncated


def test_robots_on_the_only_page_one_still_reaches_main_as_robots():
    # Reported as anything else it reads as a network fault someone could
    # fix. It is a shop saying no, and main() has the word for it.
    client = Stub({}, raises={"shop.test": crawler.Disallowed("https://shop.test")})
    with pytest.raises(crawler.Disallowed):
        scraper.fetch_html(SHOP, client)


def test_every_category_disallowed_is_the_shop_saying_no():
    shop = dict(SHOP, catalog_paths=["a", "b"])
    client = Stub({}, raises={"shop.test": crawler.Disallowed("https://shop.test")})
    with pytest.raises(crawler.Disallowed):
        scraper.fetch_html(shop, client)


# --- a challenge keeps its own word ----------------------------------------

def test_a_challenge_on_page_one_still_surfaces_as_a_challenge():
    client = Stub({}, raises={"shop.test": crawler.Challenged("https://shop.test")})
    with pytest.raises(crawler.Challenged):
        scraper.fetch_html(SHOP, client)


# --- the behaviour that must NOT change ------------------------------------

def test_a_404_past_the_end_is_the_end_of_the_catalogue_not_a_truncation():
    """Several platforms end a catalogue by 404ing the next paged URL.
    Flagging that TRUNCATED would leave every such shop permanently
    incomparable with its own stated size, which is the one cell the coverage
    table exists for."""
    client = Stub(
        {"https://shop.test": grid([1, 2, 3], "https://shop.test/?page=2")},
        raises={"page=2": crawler.UpstreamError("HTTP 404", status_code=404)})

    items = scraper.fetch_html(SHOP, client)

    assert len(items) == 3
    assert not items.truncated, "end of catalogue was reported as truncation"


# --- the JSON platforms take the same rule --------------------------------

def test_a_breaker_tripping_mid_json_walk_keeps_the_pages_already_read(
        monkeypatch, capsys):
    monkeypatch.setattr(scraper, "SHOPIFY_PAGE_SIZE", 2)
    body = ('{"products": [{"title": "Ganevat Cuvee A", "variants": '
            '[{"price": "40.00", "available": true}]}, '
            '{"title": "Ganevat Cuvee B", "variants": '
            '[{"price": "50.00", "available": true}]}]}')
    client = Stub(
        {"https://shop.test/products.json?limit=2&page=1": body},
        raises={"page=2": crawler.CircuitOpen("shop.test")})

    items = scraper.fetch_shopify(SHOP, client)

    assert len(items) == 2, "the breaker discarded a page already parsed"
    assert items.truncated
    assert "CircuitOpen" in capsys.readouterr().out


def test_a_json_shop_failing_on_page_one_still_fails():
    client = Stub({}, raises={"products.json": crawler.CircuitOpen("shop.test")})
    with pytest.raises(crawler.CircuitOpen):
        scraper.fetch_shopify(SHOP, client)


# --- and the whole way through a run ---------------------------------------

def test_a_disallowed_strip_still_leaves_the_shop_in_the_digest_and_the_pool(
        pipeline, monkeypatch, capsys):
    """The unit tests above prove the catalogue survives inside fetch_html.
    This one proves the run still *uses* it -- the failure was never a wrong
    number, it was a shop silently contributing nothing.

    The shop is built here rather than given `new_arrivals` in the shared
    canned config, which would make every other pipeline suite spend a
    request on a strip it does not care about.
    """
    import canned_shop
    html_shop = next(s for s in canned_shop.SHOPS if s["name"] == "zzz-html")
    monkeypatch.setattr(
        scraper, "SHOPS", [dict(html_shop, new_arrivals="nouveaux-produits")])

    pipeline({"https://html.test": canned_shop.html_listing(
                  [("Zzz Domaine Rouge 2020", 30, False),
                   ("Zzz Negoce Blanc 2021", 20, False),
                   ("Zzz Other Rose 2022", 25, False)])},
             disallow=("nouveaux-produits",), force=True)

    body = "\n".join(pipeline.sent)
    assert "Zzz Domaine" in body, \
        "a disallowed strip cost the shop its place in the digest"

    import json
    pool = json.loads((pipeline.tmp / "observations.json").read_text())
    assert any(r["shop"] == "zzz-html" for r in pool["records"]), \
        "a disallowed strip cost the shop its prices in the market pool"

    rows = {r["shop"]: r for r in
            json.loads((pipeline.tmp / "coverage.json").read_text())}
    assert rows["zzz-html"]["products"] == 3
    assert rows["zzz-html"]["status"] != "robots.txt", \
        "a readable shop was reported as refusing us"
    # All three watched producers are on that one page and in stock, so the
    # run has no grounds for the alias accusation. Losing the shop was what
    # produced it: "Watched but found nowhere (3)".
    assert "found nowhere" not in body, \
        "a disallowed strip turned a readable shop into an alias accusation"


def test_a_refused_document_does_not_stop_the_next_fallback(capsys):
    """`_fetch_via_pdf_list` is reached only when the HTML gave us nothing, so
    it has no partial catalogue to lose -- but it ran unguarded, and an
    exception escaping it skipped the producer-index fallback that runs on the
    next line. A shop with both would lose the half it could still read."""
    html = ('<html><body><a href="/wijnkaart.pdf">Onze wijnkaart</a>'
            '</body></html>')
    client = Stub({}, raises={".pdf": crawler.Disallowed(
        "https://shop.test/wijnkaart.pdf")})

    assert scraper._fetch_via_pdf_list(
        SHOP, html, "https://shop.test/wijnkaart", client) == []
    assert "could not be read" in capsys.readouterr().out
