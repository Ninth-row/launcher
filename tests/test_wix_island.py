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

import crawler
import scraper

FIXTURE = (pathlib.Path(__file__).parent / "fixtures"
           / "feralart-wix-island-excerpt.html")
SHOP = {"name": "feralart", "url": "https://www.feralartetvin.com"}
PAGE = "https://www.feralartetvin.com/shop"


CAPTURE = (pathlib.Path(__file__).parent.parent / "probe_pages"
           / "feralart.www-feralartetvin-com.shop.html")


def _island(records, total=None):
    """A minimal page carrying the same island shape the real one uses.

    `total` writes the `totalCount` metadata Wix puts immediately after the
    record array -- the number that says how far the walk goes.
    """
    meta = json.dumps({"list": records})
    if total is not None:
        meta = meta[:-1] + f',"totalCount":{total}}}'
    return ('<html><body><script type="application/json">'
            '{"catalog":{"category":{"productsWithMetaData":'
            + meta +
            '}}}</script></body></html>')


class _NoFetch:
    """A crawler that fails the test if the reader asks it for anything.

    Page one of an island costs no request -- it is in the HTML the catalogue
    walk already fetched -- and an island stating no total must not go
    guessing at a page two.
    """

    def get(self, url, params=None, max_age=None):
        raise AssertionError(f"the island reader should not have fetched {url}")


class _PagedWix:
    """Serves one island body per `?page=N`, and records what was asked for."""

    def __init__(self, pages, fail_on=None, error=None):
        self.pages = pages
        self.fail_on = fail_on
        self.error = error or crawler.UpstreamError("Connection reset by peer")
        self.asked = []

    def get(self, url, params=None, max_age=None):
        page = int((params or {}).get("page", 1))
        self.asked.append(page)
        if page == self.fail_on:
            raise self.error
        return crawler.FetchResult(200, self.pages.get(page, ""))


# --- the real markup ------------------------------------------------------

def test_the_real_island_parses_to_priced_in_stock_bottles():
    items = scraper._fetch_via_wix_island(SHOP, FIXTURE.read_text(), PAGE, _NoFetch())

    assert len(items) == 3
    assert [i["price"] for i in items] == [60.0, 62.0, 46.0]
    assert all(i["in_stock"] for i in items)
    assert items[0]["title"] == "2024 Makalie Spatburgunder Bleuwen"
    assert items[0]["url"] == ("https://www.feralartetvin.com/product-page/"
                               "2024-makalie-spatburgunder-bleuwen")


def test_a_page_with_no_island_reads_as_nothing_rather_than_guessing():
    assert scraper._fetch_via_wix_island(
        SHOP, "<html><body><p>no island here</p></body></html>", PAGE,
        _NoFetch()) == []
    assert scraper.wix_products("") == []


def test_a_corrupt_island_does_not_take_the_shop_down():
    broken = ('<html><body><script>{"productsWithMetaData":{"list":[{"name":'
              '"unterminated</script></body></html>')
    assert scraper._fetch_via_wix_island(SHOP, broken, PAGE, _NoFetch()) == []


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

    items = scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch())

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

    items = scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch())

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
    items = scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch())
    assert scraper.matched_aliases(items[0]["text"], "feralart") == {}


# --- the price and stock rules this project already has ------------------

def test_the_payable_price_wins_over_the_struck_through_one():
    """Wix puts the old figure in comparePrice. The price is the one you would
    pay, and comparePrice is never read."""
    html = _island([{"name": "Discounted Wine 2022", "price": 18,
                     "comparePrice": 30, "isInStock": True,
                     "urlPart": "discounted-wine-2022"}])
    assert scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch())[0]["price"] == 18.0


def test_a_zero_price_is_not_a_price():
    html = _island([{"name": "Gift Card", "price": 0, "isInStock": True,
                     "urlPart": "gift-card"}])
    assert scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch())[0]["price"] is None


def test_the_island_states_stock_outright():
    html = _island([
        {"name": "Sold Out Wine 2021", "price": 40, "isInStock": False,
         "urlPart": "sold-out-wine-2021"},
        {"name": "Available Wine 2021", "price": 40, "isInStock": True,
         "urlPart": "available-wine-2021"},
    ])
    items = scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch())
    assert [i["in_stock"] for i in items] == [False, True]


def test_silence_about_stock_is_not_sold_out():
    html = _island([{"name": "Unstated Wine 2021", "price": 40,
                     "urlPart": "unstated-wine-2021"}])
    assert scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch())[0]["in_stock"] is True


def test_a_record_with_no_slug_is_skipped_rather_than_linked_nowhere():
    html = _island([{"name": "No Slug Wine", "price": 40, "isInStock": True}])
    assert scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch()) == []


# --- catalogues are paged, and this one states how many pages it has -------

def test_the_real_capture_states_a_catalogue_far_larger_than_its_island():
    """The whole reason the walk exists, read from the shop's own page.

    `/shop` lists twelve records and states `"totalCount":923`. Reading the
    island alone is 1.3% of the shop, reported as a clean coverage row -- and
    Tom Gauditiabois, the producer this shop is watched for, is not among the
    twelve.
    """
    page = CAPTURE.read_text()

    assert len(scraper.wix_products(page)) == 12
    assert scraper.wix_catalogue_size(page) == 923


def test_a_stated_total_is_walked_to_the_end():
    pages = {n: _island([{"name": f"Wine {n}", "urlPart": f"wine-{n}",
                          "price": 20 + n, "isInStock": True}], total=4)
             for n in range(1, 5)}
    client = _PagedWix(pages)

    items = scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, client)

    # One record per page of four stated: four pages, three of them fetched,
    # because page one was already in hand.
    assert client.asked == [2, 3, 4]
    assert [i["title"] for i in items] == ["Wine 1", "Wine 2", "Wine 3", "Wine 4"]
    assert items.pages_read == 3
    assert items.pages_total == 4
    assert not items.truncated


def test_the_page_count_comes_from_the_total_not_from_a_pager_link():
    """Wix renders its pager in the browser, so there is no "next" to follow.

    The island here carries no `rel="next"` and no pager markup at all -- the
    walk has to know its length from the number the catalogue states, or it
    reads page one and calls a 90-product catalogue complete.
    """
    records = [{"name": f"Wine {n}", "urlPart": f"wine-{n}", "price": 30,
                "isInStock": True} for n in range(3)]
    pages = {n: _island(records, total=9) for n in range(1, 4)}
    client = _PagedWix(pages)

    items = scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, client)

    assert items.pages_total == 3
    assert client.asked == [2, 3]
    # Every page served the same three records, so the URL dedupe keeps three.
    assert len(items) == 3


def test_an_island_with_no_stated_total_reads_one_page_and_says_so():
    """`None` is a real answer. Walking on a hunch spends requests to learn
    nothing certain, so the reader stops -- and `_NoFetch` proves it."""
    html = _island([{"name": "Lone Wine", "urlPart": "lone", "price": 25,
                     "isInStock": True}])

    items = scraper._fetch_via_wix_island(SHOP, html, PAGE, _NoFetch())

    assert len(items) == 1
    assert items.pages_total is None
    assert items.pages_read == 0
    assert not items.truncated


def test_a_walk_cut_short_is_truncated_not_a_clean_row():
    """Nine hundred bottles unread must not print as `ok`.

    The base class, not a tuple: a breaker tripping or robots refusing
    mid-walk ends it just as surely as the budget does.
    """
    pages = {n: _island([{"name": f"Wine {n}", "urlPart": f"wine-{n}",
                          "price": 20, "isInStock": True}], total=6)
             for n in range(1, 7)}

    for error in (crawler.BudgetExceeded(PAGE), crawler.CircuitOpen(PAGE),
                  crawler.Disallowed(PAGE), crawler.Challenged(PAGE)):
        client = _PagedWix(pages, fail_on=3, error=error)

        items = scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, client)

        assert items.truncated, f"{type(error).__name__} must truncate"
        # What was already read survives: the catalogue page is in hand and
        # nothing a supplementary reader does may cost it.
        assert [i["title"] for i in items] == ["Wine 1", "Wine 2"]


def test_a_shop_that_states_more_pages_than_we_walk_is_truncated_up_front():
    records = [{"name": "Wine 1", "urlPart": "wine-1", "price": 20,
                "isInStock": True}]
    pages = {n: _island(records, total=scraper.MAX_PAGES_PER_SHOP + 50)
             for n in range(1, scraper.MAX_PAGES_PER_SHOP + 2)}
    client = _PagedWix(pages)

    items = scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, client)

    assert items.truncated
    assert max(client.asked) == scraper.MAX_PAGES_PER_SHOP


def test_a_catalogue_ending_early_is_not_truncation():
    """A platform that 404s the page after its last one is a catalogue
    ending, not a walk being cut short -- folding the two together marks such
    a shop permanently TRUNCATED and makes that cell useless.
    """
    pages = {1: _island([{"name": "Wine 1", "urlPart": "wine-1", "price": 20,
                          "isInStock": True}], total=5)}
    client = _PagedWix(pages, fail_on=2,
                       error=crawler.UpstreamError("HTTP 404", status_code=404))

    items = scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, client)

    assert not items.truncated
    assert len(items) == 1

    # The same for a page that simply states no products.
    empty = _PagedWix({1: pages[1], 2: "<html><body></body></html>"})
    items = scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, empty)
    assert not items.truncated


def test_the_stated_total_is_read_from_the_island_not_the_whole_page():
    """A Wix page carries other counted widgets. A review or category count
    read as the catalogue's size sets the walk's length from a number about
    something else."""
    html = _island([{"name": "Wine 1", "urlPart": "wine-1", "price": 20,
                     "isInStock": True}], total=3)
    noisy = html.replace("</body>", '<span>{"totalCount":9999}</span></body>')

    assert scraper.wix_catalogue_size(noisy) == 3
    assert scraper.wix_catalogue_size(
        '<html><body><p>{"totalCount":77}</p></body></html>') is None


def test_the_page_size_counts_records_the_reader_skipped():
    """A record with no name still occupied a slot on the page. Dividing the
    total by the *usable* count overstates how many pages there are, and the
    walk then asks for pages that do not exist."""
    records = [{"name": "Wine 1", "urlPart": "wine-1", "price": 20,
                "isInStock": True},
               {"name": "", "urlPart": "nameless", "price": 20}]
    pages = {1: _island(records, total=4),
             2: _island(records, total=4)}
    client = _PagedWix(pages)

    items = scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, client)

    # Four stated over two records a page is two pages, not four.
    assert items.pages_total == 2
    assert client.asked == [2]


# --- the coverage row has to learn this too -------------------------------

def test_fetch_html_reports_the_island_walk_in_its_coverage_numbers():
    """The island walk *is* this shop's catalogue walk.

    Read and reported separately, a 77-page island read would print a shop
    with one page and no stated total -- which is the clean row this whole
    change exists to stop.
    """
    shop = {"name": "feralart", "platform": "html",
            "url": "https://wix.test", "catalog_path": "shop",
            "item_selector": "div.product", "title_selector": "h2.t",
            "price_selector": "span.p"}
    pages = {n: _island([{"name": f"Wine {n}", "urlPart": f"wine-{n}",
                          "price": 20 + n, "isInStock": True}], total=3)
             for n in range(1, 4)}

    class _Client(_PagedWix):
        def get(self, url, params=None, max_age=None):
            if not (params or {}).get("page") and url.endswith("/shop"):
                # The catalogue walk's own fetch of page one.
                return crawler.FetchResult(200, self.pages[1])
            return super().get(url, params=params, max_age=max_age)

    client = _Client(pages)
    items = scraper.fetch_html(shop, client)

    assert [i["title"] for i in items] == ["Wine 1", "Wine 2", "Wine 3"]
    # One page from the catalogue walk plus the two the island reader fetched.
    assert items.pages_read == 3
    assert items.pages_total == 3
    assert not items.truncated


def test_a_gallery_that_ends_before_its_stated_total_says_so(capsys):
    """Not truncation -- nothing stopped us -- but not silence either.

    Either the total counts products the gallery does not page through, or a
    page withheld its island and the reader stopped on a shop with more to
    give. The second is the island's version of adapter drift, and a product
    count alone cannot tell them apart.
    """
    pages = {1: _island([{"name": "Wine 1", "urlPart": "wine-1", "price": 20,
                          "isInStock": True}], total=9),
             2: _island([{"name": "Wine 2", "urlPart": "wine-2", "price": 21,
                          "isInStock": True}], total=9)}
    client = _PagedWix(pages)

    items = scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, client)

    assert not items.truncated
    assert len(items) == 2
    out = capsys.readouterr().out
    assert "gallery ended at page 2 of 9 stated" in out
    assert "read 2 of 9" in out


def test_a_gallery_read_to_its_stated_end_reports_no_gap(capsys):
    pages = {n: _island([{"name": f"Wine {n}", "urlPart": f"wine-{n}",
                          "price": 20, "isInStock": True}], total=3)
             for n in range(1, 4)}

    scraper._fetch_via_wix_island(SHOP, pages[1], PAGE, _PagedWix(pages))

    assert "gallery ended" not in capsys.readouterr().out
