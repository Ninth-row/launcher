"""A shop that shows a guest the wine but not the price.

demainlesvins is PrestaShop 1.7 running private sales. Its cards carry the
producer, the vintage, the region and a "Vous devez etre connecte pour voir le
prix de ce produit" button where the figure would be -- 33 of the 34 cards on
the captured page. It was written off for that, twice, and the verdict was
wrong for the same reason vinovivo's was: it was drawn from the wrong page.

The bottle is not hidden. Only its price is, and only on the card. The product
page states it publicly, twice.
"""
from pathlib import Path

import pytest

import autoselect
import crawler
import scraper

PAGES = Path(__file__).parent.parent / "probe_pages"
LISTING = (PAGES / "demainlesvins.www-demainlesvins-com.11-la-selection.html").read_text(errors="replace")
DETAIL = (PAGES / "demainlesvins.www-demainlesvins-com."
          "la-selection-8922-domaine-la-barroche-chateauneuf-du-pape-signat.html").read_text(errors="replace")

SHOP = {"name": "demainlesvins", "platform": "html",
        "url": "https://www.demainlesvins.com",
        "item_selector": "article.product-miniature",
        "title_selector": "h2.product-title", "price_selector": "span.price"}


# --- why every earlier scan said "no prices" ---------------------------------

def test_the_page_holds_a_price_that_the_price_regex_cannot_see():
    """The euro is JSON-escaped inside an HTML attribute, so the raw markup
    contains no currency marker at all and PRICE_PATTERN reads zero prices on
    a page that plainly has one. This is the whole reason the shop was
    written off."""
    assert "€" not in DETAIL
    assert scraper.PRICE_PATTERN.findall(DETAIL) == []
    price, _ = scraper.detail_price(DETAIL)
    assert price == pytest.approx(37.50)


def test_the_detail_page_states_stock_as_a_count():
    _, in_stock = scraper.detail_price(DETAIL)
    assert in_stock is True


def test_a_page_with_no_price_says_so_rather_than_guessing():
    assert scraper.detail_price("<html><body><p>rien</p></body></html>") == (None, None)


# --- the listing ---------------------------------------------------------------

def test_the_catalogue_parses_even_though_no_card_has_a_price():
    items, how = scraper._parse_html_page(
        SHOP, LISTING, "https://www.demainlesvins.com/11-la-selection")
    assert how == "selectors"
    assert len(items) > 20
    assert all(i["title"] for i in items)
    assert all(i["price"] is None for i in items), \
        "a card with a price would mean the wall is gone -- re-read the shop"


def test_autoselect_alone_cannot_read_this_shop():
    """Which is what earns the hand-written selectors: autoselect identifies a
    card BY its currency-adjacent price, by design, and must not be loosened
    to find one here."""
    assert autoselect.find_products(
        LISTING, "https://www.demainlesvins.com/11-la-selection",
        scraper.PRICE_PATTERN, scraper.parse_price) == []


def test_a_sold_out_card_is_recognised_from_its_class_alone():
    """PrestaShop marks it with `product-oos` and nothing else -- the card's
    own text reads exactly like an available one. Without this a gone bottle
    is a find, and writing it to seen.json is what silences the restock."""
    items, _ = scraper._parse_html_page(
        SHOP, LISTING, "https://www.demainlesvins.com/11-la-selection")
    gone = [i for i in items if i.get("in_stock") is False]
    assert gone, "no card read as sold out, though the capture has one"
    assert "epuise" not in gone[0]["text"].lower()
    assert "rupture" not in gone[0]["text"].lower()


# --- the second stage ----------------------------------------------------------

class OneDetail:
    def __init__(self, body=DETAIL):
        self.body, self.urls = body, []

    def get(self, url, params=None):
        self.urls.append(url)
        return crawler.FetchResult(200, self.body)


def item(title, url):
    return {"text": title, "title": title, "price": None, "url": url,
            "variant_title": ""}


def test_only_a_watched_producer_costs_a_request():
    """The bound that makes this affordable: 4 of 432 captured titles are
    producers we watch, so a thousand-bottle catalogue is about ten requests,
    not a thousand."""
    items = [item("Domaine Ganevat Macvin du Jura", "https://x.test/7349"),
             item("Chateau Nobody Rouge 2020", "https://x.test/1"),
             item("Domaine Anonyme Blanc", "https://x.test/2")]
    client = OneDetail()
    scraper._price_from_detail_pages(SHOP, items, client)
    assert client.urls == ["https://x.test/7349"]
    assert items[0]["price"] == pytest.approx(37.50)
    assert items[1]["price"] is None and items[2]["price"] is None


def test_a_listing_that_already_has_a_price_is_never_re_fetched():
    items = [dict(item("Domaine Ganevat Macvin", "https://x.test/7349"), price=42.0)]
    client = OneDetail()
    scraper._price_from_detail_pages(SHOP, items, client)
    assert client.urls == []


def test_the_number_of_detail_fetches_is_capped():
    items = [item(f"Domaine Ganevat Cuvee {n}", f"https://x.test/{n}")
             for n in range(scraper.MAX_DETAIL_FETCHES + 10)]
    client = OneDetail()
    scraper._price_from_detail_pages(SHOP, items, client)
    assert len(client.urls) == scraper.MAX_DETAIL_FETCHES


def test_the_budget_running_out_stops_the_walk_rather_than_failing_the_shop():
    class Broke(OneDetail):
        def get(self, url, params=None):
            raise crawler.BudgetExceeded("spent")
    items = [item("Domaine Ganevat Macvin", "https://x.test/7349")]
    scraper._price_from_detail_pages(SHOP, items, Broke())   # must not raise
    assert items[0]["price"] is None


def test_a_detail_page_that_is_gone_leaves_the_listing_alone():
    class Missing(OneDetail):
        def get(self, url, params=None):
            raise crawler.UpstreamError("HTTP 404", status_code=404)
    items = [item("Domaine Ganevat Macvin", "https://x.test/7349")]
    scraper._price_from_detail_pages(SHOP, items, Missing())
    assert items[0]["price"] is None, "a 404 must not invent a price"
