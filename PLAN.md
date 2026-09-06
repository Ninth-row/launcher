# Pricing review and implementation plan

## Verdict

Your instinct is right, and the cause is specific: there are two different
pricing systems in the digest and the dominant one is broken in three ways.

Ganevat is the only producer with `lines:` in `prices.yaml`, and a banded
producer takes an early return in `evaluate.py` that never reaches
`market.py`. That path cannot ever say HIGH, it uses a different definition
of DEAL from every other producer, and it prints a number to you that is not
the number it compared. Ganevat is most of what this scraper finds, so most
of what you read comes from the broken path.

One thing I expected to find and did not: the demainlesvins title and URL
disagree, and it is not a bug. Detail in "Checked and cleared" below. Do not
spend time on it.

Everything here was reproduced offline against committed fixtures. 748 tests
pass on the branch as it stands.

## How this was tested

Every verified fixture was run through the real
`check_shop` -> `evaluate_hit` -> `market.observation` path with a stubbed
crawler and no network, producing 20 real hits, and each hit's pricing
decision was dumped with its reference, expected price and ratio. The two
PDF shops were skipped because `pypdf` cannot import in this sandbox; they
are unaffected by everything below.

## Findings

### P1. A banded producer can never be classified HIGH. Verified.

`evaluate.py:286` is the whole of it:

```python
classification="DEAL" if price750 < band else "FAIR",
```

There is no HIGH branch. Measured, same wine, same ratio, both paths:

| Listing | Path | Ratio | Class |
|---|---|---|---|
| Ganevat Chalasses at EUR 2000 | band | 25.0 | FAIR |
| Labet Chardonnay at EUR 2000 | market | 25.0 | HIGH |

Real fixture rows landing in the same trap: Ganevat "Les Grandes Teppes" at
EUR 199 against the EUR 80 domaine band, ratio 2.49, reported FAIR. Ganevat
Vin Jaune 2012 at a per-750 equivalent of EUR 142, ratio 1.78, reported FAIR.

Consequence: for the producer you watch most closely, an overpriced bottle
is indistinguishable from a correctly priced one. HIGH exists in the codebase
but is unreachable for Ganevat.

### P2. Two incompatible definitions of DEAL in one email. Verified.

The band path calls DEAL anything below the band, an implicit threshold of
1.0. The market path uses `deal_threshold: 0.85`. Measured:

| Price vs reference | Band path | Market path |
|---|---|---|
| ratio 0.988 | DEAL | FAIR |
| ratio 1.012 | FAIR | FAIR |

A bottle one percent under its reference is a DEAL if it is a Ganevat and a
FAIR if it is anything else. The word DEAL in your inbox means two different
things depending on the row.

### P3. The digest prints a number it did not compare. Verified.

`notify.py:268` prints `hit["price"]`, the raw listing price.
`notify.py:269` and `notify.py:452` print `hit["expected_price"]`, which on
the band path is the per-750 band. For anything that is not a 750ml bottle
those two numbers are not comparable, and the email computes a percentage
between them anyway (`pct = price / ref - 1`).

The live run on 23 August printed this row:

```
DEAL | Ganevat | Le Pt'iot Roukin 2023 Magnum | 1500ml | EUR 89 | EUR 80
```

Read plainly that says: costs 89, reference 80, therefore a deal. The
comparison the code actually made was 38.70 against 80. The magnum is a
genuine deal and the row gives you no way to see why. On the market path the
expected price is scaled by the format multiplier, so it is comparable; only
the band path is wrong. This is the finding most likely to be what made the
pricing feel broken.

### P4. `price_750_eur` is computed for this exact purpose and never shown.

`evaluate.py:272` sets it, and its own comment says it is
"recorded whatever happens next, so a digest row can show what the band was
actually compared against". `grep` finds zero reads in `notify.py` and
`dashboard.py`. The fix for P3 already exists as a field; nothing was wired
to it.

### P5. Vin jaune and Chateau-Chalon can only ever read FAIR.

A clavelin is 620ml with a 0.83 multiplier, so its per-750 equivalent is its
price times 1.21. Vin jaune is intrinsically dearer than the rest of the
range, so it starts above a band derived from the range as a whole. Measured:
EUR 118 becomes EUR 142.17 against an EUR 80 band. A clavelin would have to
be under EUR 66 to read DEAL. Combined with P1 the result is that the most
prized wines in the range are permanently FAIR, which is the one word that
carries no information.

### P6. `market.py` never runs for Ganevat. Not a defect, worth knowing.

The band path returns before the observed-reference ladder. The observed
price pool, which is the module the architecture document describes at
greatest length, does not inform the producer that generates most hits. That
is the intended design of `lines:`, but it means the pool's quality is
invisible in practice and the "same wine at N shops" evidence you have is
never used where you have the most data.

## Checked and cleared

**demainlesvins titles do not match their URLs, and that is the shop, not us.**
Three of three hits showed it, for example a title of "Chalasses Vieilles
Vignes Poulsard 2023 Magnum" linking to `enfant-terrible-poulsard-2016`. I
expected a card-boundary parsing bug. It is not. Across all 300 cards in the
fixture, 298 have a `data-id-product` that matches every product link inside
the card; the two that differ are unrelated. The card is internally
consistent and the slug is simply stale, because PrestaShop keeps the
original URL rewrite when a shop edits an existing product record into a
different wine. The numeric id is what resolves, so the link lands on the
right page. No change needed. Do not "fix" the parser here.

## Plan

Ordered so that the zero-risk presentation fix lands first and nothing can
lose a DEAL. Invariant: a listing that alerts today must still alert after
every step.

### Step 1. Show the number that was compared. DONE.

`notify.py`, both `format_row` and the HTML row builder.

When `price_750_eur` is present and differs from `price` by more than a
rounding step, render both: the listing price, the per-750 equivalent, and
the band. Compute the percentage from the per-750 figure against the band,
never from the raw price against the band. When the two are equal, which is
every 750ml bottle, render exactly what is rendered today.

Target for the row above: `EUR 89 (EUR 39/750ml) vs EUR 80 band, -52%`.

Tests: one row per format, 750ml unchanged, 1500ml and 620ml showing both
numbers, and one asserting the percentage is derived from the per-750 figure.
`dashboard.py` needs the same treatment only if it renders prices; check
before editing, and remember `wine.html` is generated.

Risk: none to classification. This step alone removes the appearance of
wrongness from most rows.

Shipped. `compared_price` and `_money` in `notify.py` are shared by the plain
text and HTML builders, so the two cannot drift. Rendered against real shapes:

    magnum, band path      EUR 89 (EUR 39/750ml) vs EUR 80 band (-52%)
    750ml, band path       EUR 91 vs EUR 80 band (+14%)
    clavelin, band path    EUR 118 (EUR 142/750ml) vs EUR 80 band (+78%)
    magnum, market path    EUR 200 vs EUR 184 ref (+9%)
    coffret                EUR 450

The reference is now named for what it is, a band or a ref, because those are
different claims. Seven tests, all failing against the previous code, one of
them stated against the rendered row rather than the helper so it indicts the
old output directly.

### Step 2. Give the band path a HIGH. Behaviour change, cannot lose a DEAL.

`evaluate.py` around line 286, and `prices.yaml` under each class.

Keep `DEAL` exactly as it is, `price750 < band`, so no deal that fires today
can stop firing. Add an upper bound and classify above it as HIGH, between
as FAIR. Add an optional `high_over_750_eur` per class so a human can state
it; when it is absent, derive it from the band.

**Decision needed, and I recommend the second.**

(a) Derive from the existing ratio constants: implied reference is
`band / deal_threshold`, so HIGH above `band / 0.85 * 1.25`, which is
`band * 1.47`. Consistent with the market path but the arithmetic is not
obvious to a reader.

(b) A plain multiple, `high_over_750_eur` defaulting to `band * 1.5`. On the
domaine band that makes HIGH start at EUR 120, which correctly flags the
EUR 165 Chateau-Chalon and the EUR 199 Grandes Teppes. Easier to explain in
`prices.yaml`, which is a file a human edits.

Tests: the EUR 2000 Ganevat must be HIGH; the EUR 79 and EUR 40 cases must
stay DEAL; a case just under the HIGH bound must stay FAIR. Add a test that
asserts no input that is DEAL before the change becomes non-DEAL after it,
driven from the fixture hit set.

### Step 3. Reconcile the two DEAL thresholds. Decision needed.

Once Step 2 exists, the band path has a three-way split and the remaining
inconsistency is the DEAL edge: strictly under the band, versus
`ratio <= 0.85` on the market path.

I recommend leaving the band path at "under the band" and documenting it,
because `deal_under_750_eur` means what it says and a human set that number
deliberately. If instead you want one rule everywhere, apply
`deal_threshold` to the band as well, which makes the domaine DEAL line
EUR 68 rather than EUR 80. That is a real tightening and would have
suppressed several rows in the 23 August run, so it must not be done
silently. Whichever is chosen, state it in `CLAUDE.md` next to the existing
`lines:` paragraph.

### Step 4. A band for the styles that sit above the range. Design decision.

Vin jaune, Chateau-Chalon, macvin and vin de paille are dearer by nature and
are made by both the domaine and the negoce ranges, which is exactly why
`CLAUDE.md` forbids putting them in the curated cuvee lists: as cuvees they
would outrank the label and mis-file bottles.

Proposal: a separate `styles:` map in the Ganevat `lines:` block that adjusts
only the band, never the line. Precedence stays cuvee, then label, then
default for choosing the line; the style then selects which band that line
uses. A `vin jaune` style band of, say, EUR 180 makes a EUR 142 per-750
clavelin a genuine DEAL and a EUR 250 one a HIGH, which is the information
you actually want about those wines.

Do not start this before Steps 1 and 2 are merged. It needs its own tests
proving a style word cannot move a bottle between domaine and negoce.

### Step 5. Documentation, in the same commits.

`CLAUDE.md` currently says the band path yields a "per-line threshold" and
does not say that it bypasses `market.py` or that it had no HIGH. Update the
`lines:` paragraph as part of Step 2, not afterwards.

Also correct one factual error found while reading: the architecture section
says "every priced listing is recorded to `observations.json`". Only priced
listings that matched a watched producer are recorded, because `main()`
builds observations from `all_hits`. The behaviour is right for the purpose;
the sentence is wrong.

## Not pricing, but outstanding

- **demainlesvins returned `unreachable`, 0 products, in run 160 today.** It
  read 1191 products and 7 hits on 23 August. One shop had errors that run.
  Worth a look before it is assumed to be a blip.
- **The puurwijnshop removal and the apply-config permission fix are on
  `claude/new-repo-setup-sanitize-7gerls`, unmerged.** The live run still
  reads puurwijnshop's 709 listings.
- **mesbourgognes is added but `verified: false`,** so it is skipped until a
  Probe Shops run detects its platform and replaces the placeholder fixture.
