# Overnight report

## Verdict

Green and materially more truthful than last night, confirmed on live data.
winenot reported 1228 listings in stock and 0 sold out on every run before
tonight; the dry run at 22:46 reports 1189 in stock and 32 sold out, and
Overnoy/Houillon at winenot has moved from a reported find to correctly sold
out everywhere. That is the reported bug, fixed, at zero request cost. Five defects fixed, all
of which were producing wrong output rather than no output: a shop reporting
its entire catalogue as buyable, discounted wines recorded at their
pre-discount price, four figure prices read as three, a Burgundy cru priced
against itself with the cru premium applied twice, and two spellings of one
grape treated as two wines. 729 tests pass and the fixture gate passes on six
checks. The live end to end dry run was still crawling when this was
written; it is a cold pass, which in this window takes up to 28 minutes. If
it reports an unhandled exception I will revert the change that caused it and
say so here. No new dependency, no new request, no
change to the alert rules that decide what reaches you.

The reported bottle, winenot.fr/jura/3676-ploussard.html, was the visible
edge of the first defect. It is fixed at zero request cost.

## Survey

The brief asks for seven days of run logs. The repository is three days old,
so the window is runs 46 to 64, 21 scheduled runs plus dispatches.

What actually breaks: nothing, in the sense the logs can see. Every scheduled
scraper run in the window succeeded. One Probe Shops run failed on a
merge conflict in a generated file and was fixed. Two Pages deployments were
cancelled as superseded, which is normal.

That is the problem worth naming. The scraper has been green throughout while
reporting a sold out bottle as available for days, and while every winenot
listing, all 1228 of them, read as in stock on every run. Green is not
evidence here, because nothing in the run checks whether a verdict is true.

What actually costs time: the cache, entirely. Runtime alternates between
about 2 to 4 minutes on a cache warm tick and 12 to 28 minutes on a cold one,
against a 6 hour cache TTL and a 2 hour schedule. Worst observed in the
window is run 60 at 27m56s. The deep shops dominate a cold pass: vinnouveau
walks 118 pages and winenot 103, about 69 percent of the request budget, for
roughly 30 of 133 hits.

What has never once fired: the weekly recap, because the repository is
younger than RECAP_DAYS. The restock alert had never fired before last night
because a sold out listing kept its cooldown entry, so a bottle returning
inside 30 days was silent; that was fixed and the path is now exercised by
tests but has not yet fired on live data. Eight shops have never produced a
hit: lacavedespapilles, purewijnen, puurwijnshop, zuiverwijnen, whynat,
lacaveduchateau, vinnaturel, leszinzinsduvin.

## Shipped

[IMPROVE] Stock read from the buy button, and availability read as a value.
Live evidence first: winenot went from 1228 in stock and 0 sold out to 1189
and 32, and its hit count fell from 19 to 18 because one of those hits was a
bottle nobody could buy.
winenot's theme stamps schema.org/InStock into every card and leaves the
product flags empty even on a bottle nobody can buy, so the disabled add to
cart button is the only truthful statement on the card. The button rule
refused to consult it unless the card text had already condemned every card,
which was written for a different shop. Evidence: the committed winenot
fixture now reads 11 in stock and 1 sold out, and the card it catches is
3880-blanc-de-noirs, which claims InStock in its own markup. vinnouveau's
sold out cards are now held up by markup rather than by a French label that a
restyle would remove. Every other fixture is unchanged. Zero extra requests.

[IMPROVE] The price recorded is the price you would pay. PrestaShop renders a
discounted card old price first, so the first currency adjacent number is the
one crossed out. Evidence: 9 of pangee's 36 committed cards were over stated,
including Out of Control 2021 recorded at 13.50 against a real 12.15; all
nine now record the payable price. A test pins the anti fix, because taking
the smallest number in the card would price a 729,90 pack at its 40,00
discount.

[IMPROVE] Thousands separators are part of the number. 1 600,00 EUR with a
non breaking space parsed as 600. Evidence: a table of the seven real shapes,
including both separators found in captures, now parses correctly, and a
credible price floor rejects the 1.00 that EUR 1 250,00 used to produce. The
error only ever pointed downwards, which is to say towards DEAL, and it
landed on the dearest bottles watched.

[IMPROVE] The cru multiplier is no longer applied to a reference that already
contains the cru. Evidence: the same Bonnes-Mares at two shops, ours 100 EUR
dearer, was classified DEAL with an expected price of 4950; it now reads FAIR
at an expected 1100, and the same bottle at 700 still reads DEAL.

[IMPROVE] Two spellings of one wine are one wine. Ploussard and Poulsard
scored 0.50 against a 0.60 threshold, and Les Grands Teppes VV against Grands
Teppes Vieilles Vignes scored 0.40, which is the module docstring's own
example of a pair that should match. Both now match. The reverse error is
also fixed: Cotes du Jura Chardonnay and Cotes du Jura Chardonnay Les
Chalasses scored 0.75 and were pooled, so a village bottle set the reference
for a lieu dit at nearly three times the price; they are now distinct.

[IMPROVE] Cuvees priced by a default band are named in the digest. A Ganevat
cuvee the pricebook cannot place is scored against the domaine band, which is
how a negoce bottle at a negoce price reads as a bargain. The live run names
43 of them, which is the size of the guess that was invisible until tonight:
Vin Jaune, Chateau Chalon, Vieux Macvin, Les Chonchons, Sul Q, Mon Rouge and
the rest. Each one classified against a band nobody chose for it.

[DELETE] seen.json's last_price, plus three comments that lied about it.

One further live effect worth recording, from last night's namesake change
rather than tonight's: Roumier has moved out of lavinoterie's producer list
and into watched but found nowhere. The Roumier that shop stocks is a
Burgundy namesake, not Christophe's domaine, so the shop was never a source
for the estate we watch. That ambiguity was open in yesterday's notes and is
now closed.

## Killed

Flip Ganevat's default line to negoce_unclassified. Killed by invariant 1.
It removes the false DEALs cleanly, and it also silences any real domaine
bottle whose cuvee is not among the 12 curated names, which is a false
negative and unrecoverable. Shipped the visibility half instead.

Request larger pages from the four PrestaShop shops via resultsPerPage.
Arithmetic is strong: 311 requests to about 120, roughly 11 to 15 minutes off
a cold pass. Not shipped tonight because the failure mode is a silent
half read catalogue if the pager drops the parameter, which is invariant 1
again, and it needs a per page check of the shop's own stated page size to be
safe. This is the best remaining optimisation and the one I would do next.

Fetch shops concurrently. Same reason, larger blast radius: it rewrites the
budget and clock accounting and the not reached reporting.

Detail page stock verification for matched listings. Superseded. The card
already carries the answer, so this would have spent requests to learn what
is free.

Landed cost to Denmark, per shop VAT and shipping. Killed by the config
surface criterion: it is a hand maintained table per shop.

Publish the last run to the dashboard page. Killed by the dashboard and UI
criterion.

Add Danish importer shops. Not killed on merit, and probably right on merit,
but it is a shop list change and needs your decision rather than mine.

## Deleted

seen.json's last_price field. It was written on every hit and read by nothing:
the drop rule compares last_alerted_price, which only an alert writes. Three
comments described it as feeding future price drop comparisons, which is what
made it look load bearing. Behaviour neutral, tests unchanged.

## Runtime and requests

Before: 2m06s to 27m56s per tick across runs 46 to 63, median about 4
minutes, worst 27m56s. Budget is MAX_RUN_SECONDS 2700 and a 70 minute
workflow timeout. A full cold pass is about 311 requests against a 400
request cap.

After: unchanged in requests, 0 added. Parsing costs about 15 percent more
per page for the struck through price removal and the extra stock reads,
measured on four real fixtures, which is about 6.5 seconds on a cold pass.
Worst case moves from about 27m56s to about 28m03s.

The live dry run started at 22:46 UTC on the committed tree. Status at the
time of writing: still in the crawl step, no exception so far, artifact
upload pending.

## Open question

Eight of the 19 shops have never produced a hit, and four of them are large:
lacavedespapilles at 1241 listings, purewijnen at 867, puurwijnshop at 709,
zuiverwijnen at 689. They are roughly a third of the crawl for nothing so far.
Do you want them kept as insurance against a future allocation landing there,
or should I drop the ones that have never stocked a watched producer and put
the budget into reading the productive shops more often?
