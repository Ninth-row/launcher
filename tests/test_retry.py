"""The second pass.

A first-pass failure is not evidence a shop is down. Three configured shops
answered a hand probe minutes after a run had called them unreachable, and an
unreachable shop contributes nothing: no digest rows, nothing to the price
pool, and a "watched but found nowhere" line its aliases did not earn. So a
transient failure gets exactly one more attempt once every other shop has been
read -- and a refusal gets none, because asking twice is still asking.
"""
import crawler
import scraper

from canned_shop import product, shopify, woo, woo_product

BODIES = {
    "https://shopify.test": shopify([product("Zzz Domaine Cuvee 2020", 40)]),
    "https://woo.test": woo([woo_product("Zzz Negoce Rouge 2021", 30)]),
}


def test_a_shop_that_fails_once_is_read_on_the_second_pass(pipeline, capsys):
    client = pipeline(BODIES, flaky_hosts={"shopify.test": 1})

    assert client.reopened, "the breaker was never cleared for the retry"
    out = capsys.readouterr().out
    assert "recovered on retry" in out

    rows = {r["shop"]: r for r in _coverage(pipeline)}
    assert rows["zzz-shopify"]["products"] == 1
    assert rows["zzz-shopify"]["status"] != "unreachable"


def test_the_recovered_shop_reaches_the_digest_and_the_pool(pipeline):
    pipeline(BODIES, flaky_hosts={"shopify.test": 1}, force=True)

    body = "\n".join(pipeline.sent)
    assert "Zzz Domaine" in body, "a recovered shop's hit never reached the email"

    import json
    store = json.loads((pipeline.tmp / "observations.json").read_text())
    assert any(r["shop"] == "zzz-shopify" for r in store["records"]), \
        "a recovered shop's prices never reached the market pool"


def test_one_shop_appears_once_in_coverage_after_a_retry(pipeline):
    pipeline(BODIES, flaky_hosts={"shopify.test": 1})
    names = [r["shop"] for r in _coverage(pipeline)]
    assert names.count("zzz-shopify") == 1, \
        "the retry appended a row instead of replacing the failure row"


def test_a_shop_saying_no_is_not_asked_twice(pipeline, capsys):
    client = pipeline(BODIES, challenge_hosts=("shopify.test",))

    assert client.reopened == [], "a bot challenge was retried"
    out = capsys.readouterr().out
    assert "blocked by a bot challenge" in out
    assert "recovered on retry" not in out


def test_a_shop_that_is_really_down_stays_a_failure(pipeline, capsys):
    pipeline(BODIES, fail_hosts=("shopify.test",))
    out = capsys.readouterr().out
    assert "retry failed too" in out
    rows = {r["shop"]: r for r in _coverage(pipeline)}
    assert rows["zzz-shopify"]["status"] == "unreachable"


def test_no_retry_when_the_first_pass_ran_out_of_budget(pipeline, capsys):
    # A retry must never cost a shop that has not been read once. With shops
    # left unreached, what budget remains belongs to them.
    client = pipeline(BODIES, flaky_hosts={"shopify.test": 1}, max_requests=2)
    assert client.reopened == []


def _coverage(pipeline):
    import json
    return json.loads((pipeline.tmp / "coverage.json").read_text())


def test_a_shop_that_refuses_is_not_asked_twice(pipeline, capsys):
    # mesbourgognes answered HTTP 403 to every request of a live run. That is
    # the sentence naturavin and demainlesvins said, and it is answered by not
    # going there -- a second pass would only be asking twice.

    client = pipeline(BODIES, refuse_hosts={"shopify.test": 403})
    assert client.reopened == [], "a 403 was retried"
    out = capsys.readouterr().out
    assert "refused us: HTTP 403" in out
    rows = {r["shop"]: r for r in _coverage(pipeline)}
    assert rows["zzz-shopify"]["status"] == "refused 403", \
        "a refusal still reads as an outage someone could fix"


def test_a_server_error_is_still_retried(pipeline):
    client = pipeline(BODIES, refuse_hosts={"shopify.test": 503})
    assert client.reopened, "a 503 is transient and must get a second attempt"
