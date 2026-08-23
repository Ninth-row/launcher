import pytest

import evaluate


@pytest.fixture
def pricebook():
    return {
        "defaults": {
            "format_multipliers": {375: 0.55, 750: 1.0, 1500: 2.30, 3000: 5.0},
            "burgundy_tier_multipliers": {
                "bourgogne": 0.6, "village": 1.0, "premier_cru": 2.0, "grand_cru": 4.5,
            },
            "deal_threshold": 0.85,
            "fair_ceiling": 1.25,
        },
        "producers": [
            {
                "name": "Labet",
                "region": "jura",
                "reference_750_eur": 55,
                "cuvees": [{"match": ["Fleurs", "Fleur de Marne"], "reference_750_eur": 75}],
                "last_verified": None,
                "verified": False,
            },
            {
                "name": "Roumier",
                "region": "burgundy",
                "reference_750_eur": 200,
                "cuvees": [],
                "last_verified": "2026-01-01",
                "verified": True,
            },
        ],
    }


# --- size parsing -----------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected_ml",
    [
        ("Domaine Labet Magnum 2020", 1500),
        ("Domaine Labet mag 2020", 1500),
        ("Domaine Labet 1,5L 2020", 1500),
        ("Domaine Labet 150cl 2020", 1500),
        ("Domaine Labet half bottle 2020", 375),
        ("Domaine Labet demi 2020", 375),
        ("Domaine Labet 37,5cl 2020", 375),
        # Most specific first: a Double Magnum holds the word "magnum", and
        # with 1500 tested first a 3L bottle was recorded at price/2.3
        # instead of price/5.0 -- 2.2x too high, for the 180 days an
        # observation lives.
        ("Ganevat Chalasses 2018 Double Magnum", 3000),
        ("Ganevat Chalasses 2018 Jeroboam", 3000),
    ],
)
def test_size_parsing_fixtures(text, expected_ml):
    size_ml, confidence = evaluate.parse_size(text)
    assert size_ml == expected_ml
    assert confidence == "high"


@pytest.mark.parametrize("text", [
    # A sweetness, not a format. Both of the first two are real strings from
    # committed fixtures -- pangee ships "Vin Blanc Demi-Sec", winenot ships
    # "Atemporelle Demi Sec" -- and both were read as 375ml at *high*
    # confidence. A full bottle then entered the reference pool at
    # price/0.55, an 80% inflation, and scored its own verdict against
    # expected = reference x 0.55, which is a DEAL carrying no caveat.
    "Vin Blanc Demi-Sec",
    "Atemporelle Demi Sec",
    "Cremant du Jura Demi Doux",
    "Vouvray demi-sec 2022",
])
def test_a_sweetness_is_not_a_bottle_size(text):
    """750 at low confidence, which caveats the row rather than silencing
    it. A bare "demi" still means a half bottle."""
    assert evaluate.parse_size(text) == (750, "low")


@pytest.mark.parametrize("title", [
    # Live: last night's run named "Ganevat: Pack" among the cuvees it could
    # not place. Committed: pangee sells "Le Fruit blanc 2024 ( 5 +1 offerte
    # )" at 36,00 EUR -- six bottles priced as one, EUR 6 a bottle against a
    # EUR 13 reference, which is a guaranteed DEAL, and the row then entered
    # the reference pool as though it were a single bottle.
    "Ganevat Pack decouverte",
    "Le Fruit blanc 2024 ( 5 +1 offerte )",
    "Lot de 6 bouteilles Ganevat",
    "Duo Ganevat",
])
def test_a_multi_bottle_lot_is_a_bundle(title):
    assert evaluate.is_bundle(title)


@pytest.mark.parametrize("title", [
    "Ganevat Les Chalasses 2018",
    "Ganevat Les Chalasses Marnes Bleues 2018",
    "Trousseau 2020",
])
def test_a_single_bottle_is_not_a_bundle(title):
    """The words are common enough that over-matching would caveat the whole
    catalogue and drop every format multiplier with it."""
    assert not evaluate.is_bundle(title)


def test_size_defaults_to_750_with_low_confidence_when_unmatched():
    size_ml, confidence = evaluate.parse_size("Domaine Labet Chardonnay 2020")
    assert size_ml == 750
    assert confidence == "low"


# --- tier detection ----------------------------------------------------------

def test_tier_detects_premier_cru():
    tier, confidence = evaluate.detect_tier("Chassagne-Montrachet 1er Cru Les Vergers")
    assert tier == "premier_cru"
    assert confidence == "high"


def test_tier_detects_bourgogne():
    tier, confidence = evaluate.detect_tier("Bourgogne Blanc")
    assert tier == "bourgogne"
    assert confidence == "high"


def test_tier_detects_grand_cru():
    tier, confidence = evaluate.detect_tier("Musigny Grand Cru")
    assert tier == "grand_cru"
    assert confidence == "high"


def test_tier_unknown_when_no_keyword_present():
    tier, confidence = evaluate.detect_tier("Some Unlabelled Cuvee")
    assert tier is None
    assert confidence == "low"


# --- classification ----------------------------------------------------------

def test_deal_classification(pricebook):
    hit = {"producer": "Labet", "title": "Domaine Labet Cotes du Jura Chardonnay 2020", "price": 40, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["classification"] == "DEAL"
    assert result["reference_price"] == 55
    assert result["expected_price"] == pytest.approx(55)
    assert result["caveat"] is True  # producer unverified


def test_fair_classification(pricebook):
    hit = {"producer": "Labet", "title": "Domaine Labet Cotes du Jura Chardonnay 2020", "price": 55, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["classification"] == "FAIR"


def test_high_classification(pricebook):
    hit = {"producer": "Labet", "title": "Domaine Labet Cotes du Jura Chardonnay 2020", "price": 90, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["classification"] == "HIGH"


def test_cuvee_override_reference_price_used(pricebook):
    hit = {"producer": "Labet", "title": "Domaine Labet Fleur de Marne 2020", "price": 75, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["reference_price"] == 75
    assert result["classification"] == "FAIR"


def test_burgundy_tier_multiplier_applied(pricebook):
    # Size stated explicitly (750ml) so size_confidence is "high" -- this
    # isolates the "no caveat" happy path: verified producer, confident
    # tier, confident size.
    hit = {"producer": "Roumier", "title": "Musigny Grand Cru 2018 750ml", "price": 900, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["tier"] == "grand_cru"
    assert result["expected_price"] == pytest.approx(200 * 4.5)
    assert result["classification"] == "FAIR"
    assert result["caveat"] is False  # producer verified, tier confident, size confident


def test_default_size_confidence_low_still_flags_caveat(pricebook):
    # No explicit size text -- this is the common case (most listings don't
    # spell out "750ml"), and it should still surface a caveat per the
    # "never suppress, just flag" rule, even for an otherwise-verified,
    # confidently-tiered producer.
    hit = {"producer": "Roumier", "title": "Musigny Grand Cru 2018", "price": 900, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["size_confidence"] == "low"
    assert result["classification"] != "NOREF"
    assert result["caveat"] is True


def test_magnum_applies_format_multiplier(pricebook):
    hit = {"producer": "Labet", "title": "Domaine Labet Chardonnay Magnum 2020", "price": 100, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["size_ml"] == 1500
    assert result["expected_price"] == pytest.approx(55 * 2.30)


# --- never suppress -----------------------------------------------------------

def test_unknown_producer_is_noref_but_still_returned(pricebook):
    hit = {"producer": "Some Unknown Producer", "title": "Whatever 2020", "price": 40, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["classification"] == "NOREF"
    assert result["caveat"] is True
    assert result["producer"] == "Some Unknown Producer"  # hit data preserved, not dropped


def test_missing_observed_price_is_noref_but_still_returned(pricebook):
    hit = {"producer": "Labet", "title": "Domaine Labet Chardonnay 2020", "price": None, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["classification"] == "NOREF"


def test_low_confidence_still_classifies_with_caveat(pricebook):
    # Burgundy producer, no tier keyword in the title -> tier undetected,
    # low confidence -- must still classify (not suppress) and flag caveat.
    hit = {"producer": "Roumier", "title": "Unlabelled Cuvee 2018", "price": 150, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["tier"] is None
    assert result["tier_confidence"] == "low"
    assert result["classification"] != "NOREF"  # reference exists, still classified
    assert result["caveat"] is True


def test_evaluate_hits_never_drops_a_hit(pricebook):
    hits = [
        {"producer": "Labet", "title": "Domaine Labet Chardonnay 2020", "price": 40, "shop": "s", "url": "u1"},
        {"producer": "Unknown", "title": "Whatever 2020", "price": None, "shop": "s", "url": "u2"},
    ]
    results = evaluate.evaluate_hits(hits, pricebook)
    assert len(results) == 2


# --- price parser (vintage vs. real price) -- shared with scraper.parse_price

def test_price_parser_still_ignores_vintage_in_evaluate_context():
    import scraper
    assert scraper.parse_price("Chardonnay 2020 210,00 EUR") == pytest.approx(210.0)


# --- formats and bundles seen in real listings -------------------------------
# All of these titles are verbatim from probe run 30270766108, which fetched
# real catalogues from levinnaturel and petitescaves.

def test_vin_jaune_is_a_620ml_clavelin_not_a_750():
    # Jura Vin Jaune ships in a clavelin. Scoring it as 750ml misprices it,
    # and half the tracked producers are Jura.
    assert evaluate.parse_size("Vin jaune, 2012, Jaune") == (620, "high")
    assert evaluate.parse_size("Ganevat Clavelin 62cl") == (620, "high")


@pytest.mark.parametrize("title", [
    "COFFRET ANNIVERSAIRE GANEVAT",
    'Coffret "Les Tête d\'Affiche"',
    "Caisse de 6 bouteilles",
    "Gift box assortiment",
])
def test_bundles_are_detected(title):
    assert evaluate.is_bundle(title)


@pytest.mark.parametrize("title", [
    "Les grands teppes VV, 2018, Blanc",
    "Domaine Labet Fleur de Marne 2019",
    "Vin jaune, 2012, Jaune",
])
def test_single_bottles_are_not_bundles(title):
    assert not evaluate.is_bundle(title)


def test_coffret_is_labelled_and_always_caveated(pricebook):
    # A multi-bottle box has no defensible per-bottle comparison, so it must
    # never be silently scored as a 750ml at the producer's bottle price.
    hit = {"producer": "Labet", "title": "COFFRET ANNIVERSAIRE LABET",
           "price": 498.0, "shop": "levinnaturel", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)

    assert result["bundle"] is True
    assert result["size_label"] == "coffret"
    assert result["caveat"] is True
    # No format multiplier is applied -- we don't know the bottle count.
    assert result["expected_price"] == pytest.approx(result["reference_price"])


def test_coffret_still_reported_never_suppressed(pricebook):
    hit = {"producer": "Labet", "title": "Coffret Labet", "price": 498.0,
           "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["classification"] in ("DEAL", "FAIR", "HIGH", "NOREF")


def test_clavelin_uses_its_own_format_multiplier():
    book = evaluate.load_pricebook()
    assert 620 in {int(k) for k in book["defaults"]["format_multipliers"]}


def test_size_label_present_for_plain_bottles(pricebook):
    hit = {"producer": "Labet", "title": "Labet Chardonnay Magnum 2020",
           "price": 100.0, "shop": "s", "url": "u"}
    result = evaluate.evaluate_hit(hit, pricebook)
    assert result["size_label"] == "1500ml"
    assert result["bundle"] is False
