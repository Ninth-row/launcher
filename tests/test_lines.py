"""Three things under one surname.

Ganevat bottles the domaine's own Cotes du Jura, a negoce line with his sister
Anne from bought Jura fruit, and a negoce line from fruit outside the Jura.
Their prices do not overlap -- the domaine's Chalasses is EUR 91 and the
negoce De Toute Beaute is EUR 40 -- so one pooled reference made the cheap
bottle a permanent DEAL and the dear one a permanent HIGH.

Two separate failures had to be fixed for the bands to mean anything, and both
are pinned here: the pool that could not tell the lines apart, and the absence
of any per-line threshold.
"""
import market
import notify
import evaluate
import scraper

ALIASES = scraper.PRODUCERS["Ganevat"]
PRICEBOOK = evaluate.load_pricebook()


def hit(title, price, variant=""):
    return {"producer": "Ganevat", "title": title, "price": float(price),
            "variant_title": variant, "shop": "zzz", "url": f"u/{title[:12]}"}


def scored(title, price, variant=""):
    return evaluate.evaluate_hit(hit(title, price, variant), PRICEBOOK)


# --- the pool that could not tell the lines apart -----------------------------

def test_an_ampersand_does_not_move_a_negoce_bottle_into_the_domaine_pool():
    """"&" is written for "et" and means the same thing, but it was a segment
    boundary: "Anne & Jean-Francois Ganevat" lost its "Anne" and keyed exactly
    like the domaine, while "Anne et Jean-Francois Ganevat" keyed apart. Which
    pool a negoce bottle joined depended on the shop's typography, and
    mareehaute writes it both ways in the same catalogue."""
    ampersand = market.segment("Au Sommet 2021 - Anne & Jean-François Ganevat",
                               "Ganevat", ALIASES)
    spelled = market.segment("De Toute Beauté 2024 - Anne et Jean-François Ganevat",
                             "Ganevat", ALIASES)
    domaine = market.segment("Chalasses Vieilles Vignes 2023 - Jean-François Ganevat",
                             "Ganevat", ALIASES)
    assert ampersand == spelled, "one negoce line split across two pools"
    assert ampersand != domaine, "the negoce line landed in the domaine's pool"


def test_a_format_word_is_not_part_of_the_producer_name():
    """"... 2021 Magnum - Jean-Francois Ganevat" keyed as 'magnum jean
    francois ganevat', a third line for one producer, so a cuvee's magnums were
    never compared with its bottles."""
    assert market.segment("Savagnin Les Rescapés 2021 Magnum - Jean-François Ganevat",
                          "Ganevat", ALIASES) == \
        market.segment("Savagnin Les Rescapés 2021 - Jean-François Ganevat",
                       "Ganevat", ALIASES)


# --- the bands ----------------------------------------------------------------

def test_the_domaine_band_is_eighty():
    assert scored("Chalasses Vieilles Vignes 2023 - Jean-François Ganevat", 74)["classification"] == "DEAL"
    assert scored("Chalasses Vieilles Vignes 2023 - Jean-François Ganevat", 91)["classification"] == "FAIR"


def test_the_jura_negoce_band_is_fifty_five():
    assert scored("Au Sommet 2021 - Anne & Jean-François Ganevat", 49)["classification"] == "DEAL"
    assert scored("Au Sommet 2021 - Anne & Jean-François Ganevat", 69)["classification"] == "FAIR"


def test_the_negoce_bands_do_not_borrow_each_others_thresholds():
    """A EUR 69 Jura negoce bottle is not a deal, though it would be one
    against the domaine's EUR 80 -- which is precisely the confusion that made
    every cheap Ganevat look like a find."""
    assert scored("Au Sommet 2021 - Anne & Jean-François Ganevat", 69)["classification"] == "FAIR"
    assert scored("Chalasses 2023 - Jean-François Ganevat", 69)["classification"] == "DEAL"


# --- size, which is the whole reason the bands are per 750ml -------------------

def test_a_magnum_is_judged_per_bottle_equivalent():
    """EUR 123 for a magnum is EUR 53.50 per 750ml, which is under the Jura
    negoce band. Judging the face price would have called it expensive."""
    result = scored("Rotagamète Magnum - Anne et Jean-François Ganevat", 123)
    assert result["price_750_eur"] < 55
    assert result["classification"] == "DEAL"


def test_a_clavelin_is_not_a_bargain_for_being_small():
    """A clavelin is 620ml and almost always vin jaune, which is dearer by
    nature. Comparing its face price to a 750ml band would flag EUR 90 as a
    deal; per 750ml it is EUR 108 and it is not."""
    dear = scored("Ganevat Vin Jaune Clavelin 62cl", 90)
    assert dear["price_750_eur"] > 100
    assert dear["classification"] == "FAIR"
    # And a genuinely cheap clavelin still gets through.
    assert scored("Ganevat Vin Jaune Clavelin 62cl", 55)["classification"] == "DEAL"


def test_a_coffret_is_never_banded():
    """An unknown number of bottles has no per-bottle price, so no band can
    apply -- "COFFRET ANNIVERSAIRE GANEVAT" at EUR 450 is a real listing."""
    result = scored("COFFRET ANNIVERSAIRE GANEVAT", 450)
    assert result.get("price_750_eur") is None
    assert result["bundle"] is True
    assert result["classification"] != "DEAL"


# --- the line that never alerts ------------------------------------------------

def test_negoce_from_outside_the_jura_never_alerts():
    result = scored("Anne & Jean-François Ganevat Poulprix", 25)
    assert result["line"] == "negoce_outside"
    assert result["alertable"] is False
    assert notify.should_alert(result, None, None) is False, \
        "a wine whose price says nothing about the domaine reached the inbox"


def test_a_silenced_line_is_still_recorded_and_reported():
    """Never dropped, only never alerted: evaluate.py does not suppress hits,
    and the row is worth seeing in hits.json and the digest table."""
    result = scored("Anne & Jean-François Ganevat Poulprix", 25)
    assert result["classification"] == "NOALERT"
    assert result["price"] == 25
    assert "never alerted" in result["reference_basis"]
    assert "NOALERT" in notify.SECTION_ORDER


def test_a_silenced_line_reaches_hits_json_but_not_the_email(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda body, subject=None: sent.append(body))
    hits = [scored("Anne & Jean-François Ganevat Poulprix", 25)]
    notify.run_digest(hits, state_path=tmp_path / "seen.json",
                      hits_path=tmp_path / "hits.json")
    written = (tmp_path / "hits.json").read_text()
    assert "Poulprix" in written, "the hit was dropped rather than silenced"
    assert not sent, "a silenced line was emailed"


def test_a_silenced_line_does_not_reappear_in_the_weekly_recap(tmp_path, monkeypatch):
    """The recap lists everything currently matched, which would put the
    silenced line back in the inbox once a week."""
    sent = []
    monkeypatch.setattr(notify, "send_email", lambda body, subject=None: sent.append(body))
    hits = [scored("Anne & Jean-François Ganevat Poulprix", 25)]
    state = tmp_path / "seen.json"
    state.write_text('{"_meta": {"last_recap_at": "2020-01-01T00:00:00+00:00"}}')
    notify.run_digest(hits, state_path=state, hits_path=tmp_path / "hits.json")
    assert not sent, "the recap emailed a line configured never to alert"


def test_an_unclassified_negoce_cuvee_is_silent_rather_than_guessed():
    """A negoce cuvee nobody has placed is more likely to be from outside the
    Jura than not, and that line must never alert. Silence is the safe default;
    the row still shows up to be classified."""
    result = scored("Mystère Cuvée - Anne & Jean-François Ganevat", 30)
    assert result["line"] == "negoce_unclassified"
    assert notify.should_alert(result, None, None) is False


# --- how a bottle is placed ----------------------------------------------------

def test_the_curated_cuvee_beats_the_label():
    """Shops file negoce cuvees under "Domaine Ganevat" often enough to
    matter, so the label is not the last word on what is in the bottle."""
    result = scored("Domaine Ganevat Poulprix 2022", 28)
    assert result["line"] == "negoce_outside"
    assert result["alertable"] is False


def test_anne_is_matched_as_a_word_not_a_fragment():
    """"COFFRET ANNIVERSAIRE GANEVAT" is a real listing, and "anniversaire"
    must not read as the negoce attribution."""
    line, _ = evaluate.classify_line("COFFRET ANNIVERSAIRE GANEVAT",
                                     evaluate.find_producer_entry(PRICEBOOK, "Ganevat"))
    assert line == "domaine"


def test_every_placement_says_how_it_was_decided():
    for title in ("Chalasses 2023 - Jean-François Ganevat",
                  "Au Sommet - Anne & Jean-François Ganevat",
                  "Poulprix - Anne & Jean-François Ganevat"):
        assert scored(title, 50)["line_basis"], f"no basis recorded for {title}"


# --- everyone else is untouched ------------------------------------------------

def test_a_producer_without_configured_lines_still_uses_the_observed_market():
    """The bands are a hand-set exception for one producer with three ranges.
    Nothing else may start depending on prices.yaml for a reference."""
    result = evaluate.evaluate_hit(
        {"producer": "Labet", "title": "Labet Fleur de Savagnin 2021", "price": 38.0,
         "variant_title": "", "shop": "zzz", "url": "u"}, PRICEBOOK)
    assert result.get("line") is None
    assert result["classification"] == "NOREF"      # no observed pool in this test
    assert result.get("alertable") is not False
