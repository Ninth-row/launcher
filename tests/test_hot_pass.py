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

from canned_shop import product, shopify, woo, woo_product

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
