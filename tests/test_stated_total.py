"""Sweeping a shop too big to sweep, by the number it states about itself.

feralart is 79 pages and oenovinia 45, so neither can be read every quarter
of an hour. Both state their own size on page one -- a Wix island's
`totalCount`, a PrestaShop heading's `Il y a N produits` -- and that number is
the cheapest honest signal there is: unchanged means nothing was listed or
delisted, and one request bought that answer.

The fixtures here are three real captures of feralart's /shop, trimmed to the
island and one line of Wix runtime, taken on three different days. Two of them
state 894 and one states 923, and crucially **the two 894s do not have the
same bytes** -- which is the whole reason this detector reads a number rather
than hashing the page.
"""
import hashlib
from pathlib import Path

import pytest

import scraper
from canned_shop import FakeCrawler

FIXTURES = Path(__file__).parent / "fixtures"
SHOP_URL = "https://www.feralartetvin.com/shop"


def _island(tag):
    return (FIXTURES / f"feralart-shop-island-{tag}.html").read_text()


def _shop():
    return {"name": "zzz-big", "platform": "html",
            "url": "https://www.feralartetvin.com",
            "catalog_paths": ["shop"], "hot": "stated_total",
            "item_selector": "div.product", "title_selector": "h2",
            "price_selector": "span.price", "verified": True}


def _sweep(tag, totals):
    client = FakeCrawler({SHOP_URL: _island(tag)})
    items = scraper.fetch_hot(_shop(), client, totals=totals)
    return items, client


def test_the_same_stated_total_costs_one_request_and_reads_nothing():
    totals = {"zzz-big": 894}
    items, client = _sweep("a", totals)
    assert list(items) == []
    assert client.request_count == 1, \
        "a shop whose own counter did not move was read anyway"


def test_different_bytes_with_the_same_total_still_escalate_to_nothing():
    """The test that stops anyone reintroducing a page hash.

    These two captures are the same shop on two different days. The page
    bytes differ -- Wix stamps its runtime config and bundle ids afresh on
    every response, exactly as PrestaShop stamps a random `?v=NNNNN` on every
    asset link -- while the catalogue is demonstrably unchanged at 894. A
    hash detector fires here, every time, on every shop, and degenerates into
    reading every catalogue every sweep.
    """
    a, b = _island("a"), _island("b")
    assert hashlib.sha256(a.encode()) .hexdigest() \
        != hashlib.sha256(b.encode()).hexdigest(), \
        "these fixtures no longer demonstrate byte churn"
    assert scraper.stated_catalogue_total(a) == scraper.stated_catalogue_total(b) == 894

    totals = {"zzz-big": 894}
    items, client = _sweep("b", totals)
    assert list(items) == []
    assert client.request_count == 1


def test_a_moved_total_is_read():
    totals = {"zzz-big": 894}
    _items, client = _sweep("c", totals)
    assert client.request_count > 1, \
        "the shop said its catalogue changed and nothing went to look"
    assert totals["zzz-big"] == 923


def test_the_first_sweep_records_the_number_and_reads_nothing():
    """Nothing to compare against is not a change. Treating a missing entry
    as a change would read every such catalogue on the first sweep after any
    state loss -- and state loss is already documented as silent."""
    totals = {}
    items, client = _sweep("a", totals)
    assert list(items) == []
    assert client.request_count == 1
    assert totals == {"zzz-big": 894}


def test_a_shop_that_states_no_total_says_so(capsys):
    client = FakeCrawler({SHOP_URL: "<html><body><p>no counter here</p></body></html>"})
    totals = {"zzz-big": 894}
    items = scraper.fetch_hot(_shop(), client, totals=totals)
    assert list(items) == []
    out = capsys.readouterr().out
    assert "states no total" in out, \
        "the signal disappeared and the sweep said nothing about it"


@pytest.mark.parametrize("html,expected", [
    # PrestaShop 1.6 states a total and no page size.
    ('<p>Il y a 2200 produits.</p>', 2200),
    # ...and says the same thing about the cart, earlier in the document. A
    # cart total is not a catalogue total, exactly as a cart's "0,00 EUR" is
    # not a price.
    ('<p>Il y a 0 produits dans votre panier.</p>', None),
    ('<p>nothing at all</p>', None),
])
def test_the_counters_a_shop_may_state(html, expected):
    assert scraper.stated_catalogue_total(html) == expected


def test_a_count_outside_the_island_is_not_the_catalogue_size():
    """A Wix page carries other counted widgets -- a review count, a gallery
    -- and reading one as the catalogue's size takes the sweep's whole signal
    from a number about something else. Only the metadata immediately after
    the record array counts, which is the rule wix_catalogue_size already
    enforces for the full pass's walk length."""
    assert scraper.stated_catalogue_total('{"totalCount":923}') is None
    assert scraper.stated_catalogue_total(
        '<div data-reviews=\'{"totalCount":4821}\'></div>') is None
