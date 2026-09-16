"""A canned shop of each platform, shared by the pipeline suites.

Kept out of the test modules so the end-to-end and silent-failure
suites and the pipeline fixture all describe the same imaginary shops.
"""
import json

import crawler


# --- the canned shop ----------------------------------------------------------

def shopify(products):
    return json.dumps({"products": products})


def product(title, price, available=True, vendor=""):
    return {
        "title": title, "vendor": vendor, "handle": title.lower().replace(" ", "-"),
        "body_html": "", "variants": [{"title": "Default", "price": str(price),
                                       "available": available}],
    }


def woo(products):
    return json.dumps(products)


def woo_product(name, price, in_stock=True):
    return {
        "name": name, "permalink": f"https://woo.test/p/{name.lower().replace(' ', '-')}",
        "prices": {"price": str(int(price * 100)), "currency_minor_unit": 2},
        "is_in_stock": in_stock,
    }


def html_listing(rows):
    cells = "".join(
        f'<div><a href="/p/{n}">{title}</a><span>{price},00 &euro;</span>'
        f'{"<em>Produit épuisé</em>" if sold_out else ""}</div>'
        for n, (title, price, sold_out) in enumerate(rows)
    )
    return f'<html><body><div class="grid">{cells}</div></body></html>'


SHOPS = [
    {"name": "zzz-shopify", "platform": "shopify", "url": "https://shopify.test",
     "verified": True},
    {"name": "zzz-woo", "platform": "woocommerce", "url": "https://woo.test",
     "verified": True},
    {"name": "zzz-html", "platform": "html", "url": "https://html.test",
     "item_selector": "div.product", "title_selector": "h2.product-title",
     "price_selector": "span.price", "verified": True},
    {"name": "zzz-dark", "platform": "shopify", "url": "https://dark.test",
     "verified": False},
]

PRODUCERS = {
    "Zzz Domaine": ["zzz domaine"],
    "Zzz Negoce": ["zzz negoce"],
    "Zzz Other": ["zzz other"],
}


class FakeCrawler:
    """Serves canned bodies by URL prefix and counts requests, standing in
    for the real Crawler that main() constructs for itself."""

    def __init__(self, bodies, max_requests=1000, fail_hosts=(),
                 flaky_hosts=None, challenge_hosts=(), refuse_hosts=None):
        self.bodies = bodies
        self.max_requests = max_requests
        self.request_count = 0
        self.fail_hosts = set(fail_hosts)
        # host substring -> how many more requests fail before it answers.
        # This is what a shop that refuses a sustained crawl and then recovers
        # after a pause looks like from inside the run.
        self.flaky_hosts = dict(flaky_hosts or {})
        self.challenge_hosts = set(challenge_hosts)
        # host substring -> the status it answers with. A shop that answers is
        # not a shop that failed, and the run has to tell the two apart.
        self.refuse_hosts = dict(refuse_hosts or {})
        self.reopened = []
        self.urls = []

    def reopen(self, url):
        self.reopened.append(url)

    def get(self, url, params=None):
        self.request_count += 1
        self.urls.append(url)
        if self.request_count > self.max_requests:
            raise crawler.BudgetExceeded(url)
        for host in self.fail_hosts:
            if host in url:
                raise crawler.UpstreamError("Connection refused")
        for host in self.challenge_hosts:
            if host in url:
                raise crawler.Challenged(url)
        for host, code in self.refuse_hosts.items():
            if host in url:
                raise crawler.UpstreamError(f"HTTP {code}", status_code=code)
        for host, left in self.flaky_hosts.items():
            if host in url and left > 0:
                self.flaky_hosts[host] = left - 1
                raise crawler.UpstreamError("Connection reset by peer")
        page = int((params or {}).get("page", 1))
        for prefix, body in self.bodies.items():
            if url.startswith(prefix):
                if page > 1:
                    return crawler.FetchResult(
                        200, '{"products": []}' if "shopify" in prefix
                        else "[]" if "woo" in prefix else "")
                return crawler.FetchResult(200, body)
        return crawler.FetchResult(200, "")
