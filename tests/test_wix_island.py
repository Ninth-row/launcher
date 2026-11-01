"""A Wix shop states its catalogue in JSON, not in cards.

feralart probed as "responded and parsed, but zero products found" on every
path that answered, and the verdict was wrong the same way vinovivo's and
demainlesvins' were: the page it was read from held 40 currency markers and 25
product links all along. Wix renders its grid in the browser from a JSON
island, so there is no repeated priced structure for `autoselect` to climb --
and the island is a *better* source than a grid, because the price is numeric,
the currency is named and the stock is a flag rather than a French word.

`tests/fixtures/feralart-wix-island-excerpt.html` keeps three of the shop's
twelve records verbatim.
"""
import json
import pathlib

import scraper

FIXTURE = (pathlib.Path(__file__).parent / "fixtures"
           / "feralart-wix-island-excerpt.html")
SHOP = {"name": "feralart", "url": "https://www.feralartetvin.com"}
PAGE = "https://www.feralartetvin.com/shop"


def _island(records):
    """A minimal page carrying the same island shape the real one uses."""
    return ('<html><body><script type="application/json">'
            '{"catalog":{"category":{"productsWithMetaData":'
            + json.dumps({"list": records}) +
            '}}}</script></body></html>')


# --- the real markup ------------------------------------------------------

def test_the_real_island_parses_to_priced_in_stock_bottles():
    items = scraper._fetch_via_wix_island(SHOP, FIXTURE.read_text(), PAGE)

    assert len(items) == 3
    assert [i["price"] for i in items] == [60.0, 62.0, 46.0]
    assert all(i["in_stock"] for i in items)
    assert items[0]["title"] == "2024 Makalie Spatburgunder Bleuwen"
    assert items[0]["url"] == ("https://www.feralartetvin.com/product-page/"
                               "2024-makalie-spatburgunder-bleuwen")


def test_a_page_with_no_island_reads_as_nothing_rather_than_guessing():
    assert scraper._fetch_via_wix_island(
        SHOP, "<html><body><p>no island here</p></body></html>", PAGE) == []
    assert scraper.wix_products("") == []


def test_a_corrupt_island_does_not_take_the_shop_down():
    broken = ('<html><body><script>{"productsWithMetaData":{"list":[{"name":'
              '"unterminated</script></body></html>')
    assert scraper._fetch_via_wix_island(SHOP, broken, PAGE) == []


# --- the trap this shop sets ---------------------------------------------

def test_a_book_about_a_grower_is_not_a_bottle_of_that_grower():
    """feralart sells publications and candles whose blurbs name growers. Its
    real listing "A Vif - Terroirs en Feu" describes itself as "covering Julie
    Balagny, L'Arbe Blanc, Labet, Aurelien Lefort, Pedres Blanques and many
    more" -- and `matched_aliases` returns Labet for that sentence. Matching
    the description would file a book as a bottle at the book's price, which
    is the "a blog post about Overnoy is not a bottle of Overnoy" rule.
    """
    html = _island([{
        "name": "A Vif - Terroirs en Feu",
        "description": ("After Fusee Bordeaux and Burgundy comes the next "
                        "edition - covering Julie Balagny, L'Arbe Blanc, "
                        "Labet, Aurelien Lefort, Pedres Blanques and many "
                        "more."),
        "price": 25, "isInStock": True, "urlPart": "a-vif-terroirs-en-feu",
    }])

    items = scraper._fetch_via_wix_island(SHOP, html, PAGE)

    assert len(items) == 1
    assert items[0]["text"] == "A Vif - Terroirs en Feu", \
        "the description reached the matcher and will mint a false Labet hit"
    assert scraper.matched_aliases(items[0]["text"], "feralart") == {}
    # ... while the sentence itself plainly would have matched:
    assert "Labet" in scraper.matched_aliases(
        items[0]["title"] + " " +
        "covering Julie Balagny, L'Arbe Blanc, Labet, Aurelien Lefort")


def test_a_watched_producer_in_the_name_is_still_found():
    html = _island([{
        "name": "Tom Gauditiabois El03 Champagne",
        "price": 95, "isInStock": True,
        "urlPart": "tom-gauditiabois-el03-champagne",
    }])

    items = scraper._fetch_via_wix_island(SHOP, html, PAGE)

    assert scraper.matched_aliases(items[0]["text"], "feralart") == {
        "Tom Gauditiabois": "tom gauditiabois"}


def test_the_namesake_rule_still_holds_here():
    """We watch Jules Brochet. feralart stocks Champagne Emmanuel Brochet,
    which is a different estate -- and the alias is the full name, so it must
    not match."""
    html = _island([{
        "name": "Champagne Emmanuel Brochet 2016 Les Hauts Meunier",
        "price": 80, "isInStock": True, "urlPart": "emmanuel-brochet-2016",
    }])
    items = scraper._fetch_via_wix_island(SHOP, html, PAGE)
    assert scraper.matched_aliases(items[0]["text"], "feralart") == {}


# --- the price and stock rules this project already has ------------------

def test_the_payable_price_wins_over_the_struck_through_one():
    """Wix puts the old figure in comparePrice. The price is the one you would
    pay, and comparePrice is never read."""
    html = _island([{"name": "Discounted Wine 2022", "price": 18,
                     "comparePrice": 30, "isInStock": True,
                     "urlPart": "discounted-wine-2022"}])
    assert scraper._fetch_via_wix_island(SHOP, html, PAGE)[0]["price"] == 18.0


def test_a_zero_price_is_not_a_price():
    html = _island([{"name": "Gift Card", "price": 0, "isInStock": True,
                     "urlPart": "gift-card"}])
    assert scraper._fetch_via_wix_island(SHOP, html, PAGE)[0]["price"] is None


def test_the_island_states_stock_outright():
    html = _island([
        {"name": "Sold Out Wine 2021", "price": 40, "isInStock": False,
         "urlPart": "sold-out-wine-2021"},
        {"name": "Available Wine 2021", "price": 40, "isInStock": True,
         "urlPart": "available-wine-2021"},
    ])
    items = scraper._fetch_via_wix_island(SHOP, html, PAGE)
    assert [i["in_stock"] for i in items] == [False, True]


def test_silence_about_stock_is_not_sold_out():
    html = _island([{"name": "Unstated Wine 2021", "price": 40,
                     "urlPart": "unstated-wine-2021"}])
    assert scraper._fetch_via_wix_island(SHOP, html, PAGE)[0]["in_stock"] is True


def test_a_record_with_no_slug_is_skipped_rather_than_linked_nowhere():
    html = _island([{"name": "No Slug Wine", "price": 40, "isInStock": True}])
    assert scraper._fetch_via_wix_island(SHOP, html, PAGE) == []
