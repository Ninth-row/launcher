"""Price-band evaluation: bottle size, Burgundy cru tier, and comparison
against the reference figures in prices.yaml.

Never suppresses a hit. A missing reference, an unverified reference, or
low size/tier confidence gets a caveat marker, not a filter -- filtering on
bad reference data would hide real finds, which defeats the point of the
scraper.
"""
import re
from pathlib import Path

import yaml

import market
import textnorm

PRICES_PATH = Path(__file__).parent / "prices.yaml"


# One shared implementation (textnorm), so this can no longer drift from
# scraper's. Accent-only on purpose: BUNDLE_RE and the cru patterns read
# punctuation.
normalize = textnorm.strip_accents


def load_pricebook(path=None):
    with open(path or PRICES_PATH) as f:
        return yaml.safe_load(f)


# --- bottle size ----------------------------------------------------------

SIZE_PATTERNS = [
    (375, re.compile(r"\b(?:half(?:\s*bottle)?|demie?(?:-?\s*bouteille)?|37[.,]?5\s*cl|375\s*ml)\b", re.I)),
    (1500, re.compile(r"\b(?:magnum|mag\.?|1[.,]5\s*l|150\s*cl|1500\s*ml)\b", re.I)),
    (3000, re.compile(r"\b(?:double\s*magnum|jeroboam|3[.,]0?\s*l|300\s*cl|3000\s*ml)\b", re.I)),
    (750, re.compile(r"\b(?:75\s*cl|750\s*ml)\b", re.I)),
    # Jura Vin Jaune ships in a 620ml clavelin. Half the tracked
    # producers are Jura, so treating one as 750ml misprices it by ~20%.
    (620, re.compile(r"\b(?:clavelin|62\s*cl|620\s*ml|vin\s+jaune)\b", re.I)),
]


def parse_size(text):
    """Return (size_ml, confidence). Defaults to (750, "low") when nothing
    in the text matches a known format."""
    text = text or ""
    for size, pattern in SIZE_PATTERNS:
        if pattern.search(text):
            return size, "high"
    return 750, "low"


# A coffret/case is several bottles in a box, so its price is not
# comparable to a per-bottle reference at all. Real listings from
# levinnaturel and petitescaves ("COFFRET ANNIVERSAIRE GANEVAT", EUR 450)
# would otherwise be scored against a ~EUR 70 single-bottle reference and
# shouted about as HIGH. Detect them and caveat instead of pretending.
BUNDLE_RE = re.compile(
    r"\b(?:coffret|caisse|carton|case\s+of|gift\s*(?:box|set)|"
    r"(?:\d+)\s*(?:bouteilles|bottles)|assortiment|panach\w+)\b",
    re.I,
)


def is_bundle(text):
    return bool(BUNDLE_RE.search(normalize(text or "")))


# --- Burgundy tier ----------------------------------------------------------

GRAND_CRU_RE = re.compile(r"grand\s*cru", re.I)
PREMIER_CRU_RE = re.compile(r"(premier\s*cru|\b1er\b)", re.I)
BOURGOGNE_RE = re.compile(r"\bbourgogne\b", re.I)
VILLAGE_APPELLATIONS = [
    "chambolle-musigny", "chambolle musigny", "gevrey-chambertin", "gevrey chambertin",
    "vosne-romanee", "vosne romanee", "nuits-saint-georges", "nuits saint georges",
    "chassagne-montrachet", "chassagne montrachet", "puligny-montrachet", "puligny montrachet",
    "morey-saint-denis", "morey saint denis", "volnay", "pommard", "meursault", "beaune",
    "marsannay", "fixin", "santenay", "auxey-duresses", "auxey duresses",
]


def detect_tier(text):
    """Return (tier, confidence) for a Burgundy cuvee string. tier is one of
    grand_cru/premier_cru/village/bourgogne, or None if undetected (with
    confidence "low")."""
    norm = normalize(text)
    if GRAND_CRU_RE.search(norm):
        return "grand_cru", "high"
    if PREMIER_CRU_RE.search(norm):
        return "premier_cru", "high"
    if BOURGOGNE_RE.search(norm):
        return "bourgogne", "high"
    if any(v in norm for v in VILLAGE_APPELLATIONS):
        return "village", "high"
    return None, "low"


# --- reference lookup -------------------------------------------------------

def find_producer_entry(pricebook, producer_name):
    for entry in pricebook.get("producers", []):
        if entry.get("name") == producer_name:
            return entry
    return None


def find_cuvee_override(producer_entry, text):
    norm = normalize(text)
    for cuvee in (producer_entry or {}).get("cuvees") or []:
        if any(normalize(m) in norm for m in cuvee.get("match", [])):
            return cuvee
    return None


def derive_cuvee(title, producer_name):
    """Best-effort display label: the title with the producer's own name
    words stripped out. Cosmetic only -- doesn't affect classification."""
    label = title or ""
    for word in (producer_name or "").replace("/", " ").split():
        label = re.sub(re.escape(word), "", label, flags=re.IGNORECASE)
    label = re.sub(r"\s{2,}", " ", label).strip(" -,")
    return label or title or ""


# --- evaluation --------------------------------------------------------------


# --- which line of a producer's range a bottle belongs to ---------------------
#
# Ganevat sells three different things under one surname: the domaine's own
# Cotes du Jura, a negoce line bottled with his sister Anne from bought Jura
# fruit, and a negoce line from fruit outside the Jura entirely. Their prices
# do not overlap (EUR 91 domaine, EUR 40 negoce), so one pooled reference
# makes the cheap bottle a permanent DEAL and the dear one a permanent HIGH --
# which is what this exists to stop.
#
# The bands are absolute, per 750ml equivalent, and configured by hand in
# prices.yaml. That is a deliberate exception to "references are observed":
# a human who knows the range set these, and the observed pool cannot
# separate what a label does not distinguish.
LINE_UNPLACED_BASIS = "line not classified"


def classify_line(title, producer_entry):
    """(line name, how we know) for this bottle, or (None, None).

    Order of trust: the curated cuvee list, then the label's attribution, then
    the configured default. The list comes first because shops file negoce
    cuvees under "Domaine Ganevat" often enough to matter -- the label is not
    always the truth about what is in the bottle.
    """
    lines = (producer_entry or {}).get("lines") or {}
    if not lines:
        return None, None
    norm = normalize(title)

    for line_name, cuvees in (lines.get("cuvees") or {}).items():
        for cuvee in cuvees or []:
            if normalize(cuvee) in norm:
                return line_name, f"cuvee {cuvee!r}"

    for mark in lines.get("negoce_marks") or []:
        # Whole word: "anne" must not match "anniversaire", and a coffret
        # called ANNIVERSAIRE GANEVAT is a real listing.
        if re.search(rf"\b{re.escape(normalize(mark))}\b", norm):
            return (lines.get("negoce_default") or "negoce_unclassified",
                    f"label says {mark!r}")

    return lines.get("default"), "default for this producer"


def band_for(line_name, producer_entry):
    """The configured band for a line: (deal_under_750, alertable)."""
    classes = ((producer_entry or {}).get("lines") or {}).get("classes") or {}
    entry = classes.get(line_name) or {}
    return entry.get("deal_under_750_eur"), entry.get("alert", True)


def evaluate_hit(hit, pricebook, market_store=None, aliases=None):
    """Return a new dict: hit plus size_ml, size_confidence, tier,
    tier_confidence, reference_price, expected_price, ratio, classification,
    reference_verified, caveat, cuvee.

    Always returns a fully classified hit -- never None, never dropped.
    """
    defaults = pricebook.get("defaults", {})
    format_multipliers = {int(k): v for k, v in (defaults.get("format_multipliers") or {}).items()}
    tier_multipliers = defaults.get("burgundy_tier_multipliers") or {}
    deal_threshold = defaults.get("deal_threshold", 0.85)
    fair_ceiling = defaults.get("fair_ceiling", 1.25)

    size_text = f"{hit.get('title', '')} {hit.get('variant_title', '')}"
    bundle = is_bundle(size_text)
    size_ml, size_confidence = parse_size(size_text)
    if bundle:
        # Unknown bottle count, so no format multiplier is defensible.
        # Report it as a coffret and always caveat it.
        size_confidence = "low"
        format_multiplier = 1.0
    else:
        format_multiplier = format_multipliers.get(size_ml, 1.0)

    result = dict(hit)
    result["size_ml"] = size_ml
    result["size_confidence"] = size_confidence
    result["bundle"] = bundle
    result["size_label"] = "coffret" if bundle else f"{size_ml}ml"
    result["cuvee"] = derive_cuvee(hit.get("title", ""), hit.get("producer", ""))

    observed = None
    if market_store is not None:
        observed = market.reference_from_market(
            result, market_store, format_multipliers, aliases or {}
        )

    producer_entry = find_producer_entry(pricebook, hit.get("producer"))
    if producer_entry is None:
        # No pricebook row is not the same as no reference. Returning NOREF
        # here threw away the observed cross-shop price computed just above,
        # so a producer watched in PRODUCERS but absent from prices.yaml
        # could never be priced -- even with the same bottle listed at three
        # other shops. An empty entry falls through to exactly the right
        # place: no manual override, no region, so the observed reference is
        # used, and NOREF still results when there is nothing to observe.
        producer_entry = {}

    reference_verified = bool(producer_entry.get("verified", False))

    # A configured line replaces the observed reference for this producer, and
    # can silence a line entirely. The hit is still returned and still reaches
    # hits.json and the digest table -- only the alert is withheld, which is
    # notify's decision to make, not this module's.
    line_name, line_basis = classify_line(hit.get("title", ""), producer_entry)
    if line_name:
        result["line"] = line_name
        result["line_basis"] = line_basis
        band, alertable = band_for(line_name, producer_entry)
        price = hit.get("price")
        # Per 750ml, so a magnum is not a bargain for being big and a clavelin
        # is not one for being small. A coffret has no per-bottle price at all,
        # so it is never banded.
        price750 = None if bundle or price is None else market.to_750(
            price, size_ml or 750, format_multipliers)
        # Recorded whatever happens next, so a digest row can show what the
        # band was actually compared against. None for a coffret: an unknown
        # number of bottles has no per-bottle price.
        result["price_750_eur"] = price750
        if not alertable:
            result.update(
                tier=None, tier_confidence="n/a", reference_price=None,
                expected_price=None, ratio=None, classification="NOALERT",
                reference_basis=f"{line_name} ({line_basis}) is never alerted",
                reference_shops=[], reference_verified=True, caveat=True,
                alertable=False, price_750_eur=price750,
            )
            return result
        if band is not None and price750 is not None:
            result.update(
                tier=None, tier_confidence="n/a", reference_price=band,
                expected_price=band, ratio=round(price750 / band, 3),
                classification="DEAL" if price750 < band else "FAIR",
                reference_basis=(f"{line_name} band: deal under EUR {band:g} "
                                 f"per 750ml ({line_basis})"),
                reference_shops=[], reference_verified=True,
                caveat=size_confidence == "low", alertable=True,
                price_750_eur=price750,
            )
            return result

    tier = None
    tier_confidence = "n/a"
    tier_multiplier = 1.0
    if producer_entry.get("region") == "burgundy":
        tier, tier_confidence = detect_tier(hit.get("title", ""))
        if tier and tier in tier_multipliers:
            tier_multiplier = tier_multipliers[tier]
        else:
            tier = None  # unknown tier -> no multiplier applied

    cuvee_override = find_cuvee_override(producer_entry, hit.get("title", ""))
    manual_price = (cuvee_override or {}).get("reference_750_eur", producer_entry.get("reference_750_eur"))

    # Order of trust: a figure a human checked, then what other shops
    # actually charge, then an unchecked placeholder. The placeholders are
    # last on purpose -- one guessed number per producer is what made a
    # negoce bottle look like a permanent bargain.
    reference_price, basis, basis_confidence = None, None, "n/a"
    if manual_price is not None and reference_verified:
        reference_price, basis, basis_confidence = manual_price, "verified by hand", "high"
    elif observed is not None:
        reference_price = observed["price"]
        basis, basis_confidence = observed["basis"], observed["confidence"]
        reference_verified = observed["confidence"] == "high"
        # A reference drawn from the same cuvee elsewhere already contains
        # the cru: it *is* a Bonnes-Mares price. Multiplying it by the
        # grand-cru factor again valued a EUR 1100 bottle at EUR 4950, so the
        # identical wine EUR 100 dearer than our only comparison came out
        # DEAL. Fires deterministically the moment two shops list the same
        # Burgundy cru -- and every burgundy producer watched is unverified,
        # so the observed figure is always the one used. A producer-level
        # reference (the line median, or a hand-entered figure) is the case
        # the multiplier was written for, and keeps it.
        if observed.get("level") == "cuvee":
            tier_multiplier = 1.0
    elif manual_price is not None:
        reference_price, basis, basis_confidence = manual_price, "unverified placeholder", "low"

    result["reference_basis"] = basis
    result["reference_shops"] = (observed or {}).get("shops", [])

    if reference_price is None:
        result.update(
            tier=tier, tier_confidence=tier_confidence, reference_price=None,
            expected_price=None, ratio=None, classification="NOREF",
            reference_verified=reference_verified, caveat=True,
        )
        return result

    expected_price = reference_price * tier_multiplier * format_multiplier
    observed_price = hit.get("price")

    if observed_price is None or not expected_price:
        classification = "NOREF"
        ratio = None
    else:
        ratio = observed_price / expected_price
        if ratio <= deal_threshold:
            classification = "DEAL"
        elif ratio > fair_ceiling:
            classification = "HIGH"
        else:
            classification = "FAIR"

    low_confidence = (size_confidence == "low" or tier_confidence == "low"
                      or basis_confidence in ("low", "medium"))
    caveat = (not reference_verified) or low_confidence or bundle

    result.update(
        tier=tier,
        tier_confidence=tier_confidence,
        reference_price=reference_price,
        expected_price=expected_price,
        ratio=ratio,
        classification=classification,
        reference_verified=reference_verified,
        caveat=caveat,
    )
    return result


def evaluate_hits(hits, pricebook=None, market_store=None, aliases=None):
    pricebook = pricebook if pricebook is not None else load_pricebook()
    return [evaluate_hit(hit, pricebook, market_store, aliases) for hit in hits]
