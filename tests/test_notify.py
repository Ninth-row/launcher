import json
from datetime import datetime, timedelta, timezone

import pytest

import notify


def make_hit(**overrides):
    hit = {
        "shop": "example-shop",
        "producer": "Labet",
        "cuvee": "Cotes du Jura Chardonnay",
        "title": "Domaine Labet Cotes du Jura Chardonnay 2020",
        "price": 55.0,
        "url": "https://example-shop.test/products/labet-chardonnay",
        "variant_title": "",
        "size_ml": 750,
        "expected_price": 55.0,
        "classification": "FAIR",
        "caveat": False,
    }
    hit.update(overrides)
    return hit


# --- cooldown / dedupe --------------------------------------------------------

def test_new_item_always_alerts():
    hit = make_hit(classification="DEAL")
    alerting, state = notify.select_alerts([hit], state={})
    assert alerting == [hit]
    key = notify.item_key(hit)
    assert state[key]["last_alerted_price"] == 55.0


def test_same_item_twice_in_a_row_alerts_once():
    hit = make_hit(classification="DEAL")
    now = datetime.now(timezone.utc)

    alerting1, state = notify.select_alerts([hit], state={}, now=now)
    assert len(alerting1) == 1

    # Same item, same price, same classification, seen again shortly after.
    alerting2, state = notify.select_alerts([hit], state=state, now=now + timedelta(minutes=5))
    assert alerting2 == []


def test_a_price_drop_alerts_inside_the_cooldown():
    """Reversed decision. This used to assert the cooldown won, on the
    reasoning that it stops the digest repeating itself. It does not -- an
    unchanged item never re-alerts either way, because the post-cooldown
    branch also requires news. All the 30-day gate did to a price drop was
    delay it by up to a month, which is how a live tracker goes silent for
    weeks while a wine we already reported halves in price."""
    hit1 = make_hit(price=100.0, classification="FAIR")
    now = datetime.now(timezone.utc)
    alerting1, state = notify.select_alerts([hit1], state={}, now=now)
    assert len(alerting1) == 1

    # One day later, well inside the cooldown, but >10% cheaper.
    hit2 = make_hit(price=85.0, classification="DEAL")
    alerting2, state = notify.select_alerts([hit2], state=state, now=now + timedelta(days=1))
    assert len(alerting2) == 1


def test_a_shallow_price_drop_stays_silent_inside_the_cooldown():
    hit1 = make_hit(price=100.0)
    now = datetime.now(timezone.utc)
    _, state = notify.select_alerts([hit1], state={}, now=now)

    alerting, state = notify.select_alerts(
        [make_hit(price=95.0)], state=state, now=now + timedelta(days=1))
    assert alerting == [], "a 5% move is noise, not news"


def test_a_price_rise_stays_silent_inside_the_cooldown():
    now = datetime.now(timezone.utc)
    _, state = notify.select_alerts([make_hit(price=100.0)], state={}, now=now)
    alerting, _ = notify.select_alerts(
        [make_hit(price=130.0)], state=state, now=now + timedelta(days=1))
    assert alerting == []


def test_each_alert_resets_the_drop_baseline():
    """What stops an hourly re-alert: the comparison is against the price we
    last alerted at, not the original one. A decline alerts once per further
    -10% step, and a round trip alerts not at all."""
    now = datetime.now(timezone.utc)
    _, state = notify.select_alerts([make_hit(price=100.0)], state={}, now=now)

    alerting, state = notify.select_alerts(
        [make_hit(price=85.0)], state=state, now=now + timedelta(hours=1))
    assert len(alerting) == 1                      # baseline is now 85

    alerting, state = notify.select_alerts(
        [make_hit(price=80.0)], state=state, now=now + timedelta(hours=2))
    assert alerting == [], "-6% from the last alert is not another find"

    alerting, state = notify.select_alerts(
        [make_hit(price=76.0)], state=state, now=now + timedelta(hours=3))
    assert len(alerting) == 1

    # Back up to 85 and down to 80 again: nothing, the baseline is 76.
    _, state = notify.select_alerts(
        [make_hit(price=85.0)], state=state, now=now + timedelta(hours=4))
    alerting, _ = notify.select_alerts(
        [make_hit(price=80.0)], state=state, now=now + timedelta(hours=5))
    assert alerting == []


def test_a_classification_improvement_still_waits_for_the_cooldown():
    """Deliberately not relaxed with the price rule. Classification is
    derived from the observed market pool, which moves every hour as other
    shops are crawled, so DEAL -> FAIR -> DEAL flapping is realistic in a
    way a price round trip is not."""
    now = datetime.now(timezone.utc)
    _, state = notify.select_alerts(
        [make_hit(price=100.0, classification="FAIR")], state={}, now=now)

    alerting, _ = notify.select_alerts(
        [make_hit(price=100.0, classification="DEAL")],
        state=state, now=now + timedelta(days=3))
    assert alerting == []


def test_price_drop_over_10_percent_alerts_once_cooldown_has_elapsed():
    hit1 = make_hit(price=100.0, classification="FAIR")
    now = datetime.now(timezone.utc)
    alerting1, state = notify.select_alerts([hit1], state={}, now=now)
    assert len(alerting1) == 1

    hit2 = make_hit(price=85.0, classification="DEAL")
    alerting2, state = notify.select_alerts([hit2], state=state, now=now + timedelta(days=31))
    assert len(alerting2) == 1


def test_classification_improved_to_deal_alerts_once_seen_but_never_alerted():
    # Seen before (e.g. was NOREF/FAIR, never crossed the alert bar), then
    # improves to DEAL -- should alert even though nothing was ever alerted.
    now = datetime.now(timezone.utc)
    hit1 = make_hit(price=100.0, classification="FAIR")
    _, state = notify.select_alerts([], state={})  # start empty
    state[notify.item_key(hit1)] = {"last_price": 100.0}  # seen, never alerted

    hit2 = make_hit(price=90.0, classification="DEAL")
    alerting, state = notify.select_alerts([hit2], state=state, now=now)
    assert len(alerting) == 1


def test_unchanged_deal_does_not_re_alert_within_cooldown():
    hit = make_hit(price=40.0, classification="DEAL")
    now = datetime.now(timezone.utc)
    alerting1, state = notify.select_alerts([hit], state={}, now=now)
    assert len(alerting1) == 1

    # Still DEAL, same price, a few days later -- no re-alert.
    alerting2, state = notify.select_alerts([hit], state=state, now=now + timedelta(days=10))
    assert alerting2 == []


# --- digest formatting ---------------------------------------------------------

def test_build_digest_body_sections_in_order_and_high_last():
    hits = [
        make_hit(classification="HIGH", producer="Roumier"),
        make_hit(classification="DEAL", producer="Labet"),
        make_hit(classification="NOREF", producer="Unknown"),
        make_hit(classification="FAIR", producer="Ganevat"),
    ]
    body = notify.build_digest_body(hits)
    deal_idx = body.index("DEAL")
    fair_idx = body.index("FAIR ")
    noref_idx = body.index("NOREF")
    high_heading_idx = body.index("Flagged as overpriced")
    assert deal_idx < fair_idx < noref_idx < high_heading_idx


def test_caveat_row_gets_asterisk_and_footnote():
    hits = [make_hit(classification="DEAL", caveat=True)]
    body = notify.build_digest_body(hits)
    assert "DEAL*" in body
    assert "reference unverified" in body


def test_no_caveat_no_footnote():
    hits = [make_hit(classification="DEAL", caveat=False)]
    body = notify.build_digest_body(hits)
    assert "DEAL " in body
    assert "reference unverified" not in body


def test_email_capped_at_40_rows():
    hits = [make_hit(classification="DEAL", url=f"https://example.test/{i}") for i in range(50)]
    body = notify.build_digest_body(hits)
    assert body.count("https://example.test/") == 40
    assert "10 more alert-worthy hit(s) omitted" in body


def test_header_row_present():
    body = notify.build_digest_body([make_hit(classification="DEAL")])
    assert "STATUS | Producer | Cuvee | Size | Price | Ref | Basis | Link" in body


# --- silent run ------------------------------------------------------------------

def test_run_digest_silent_when_nothing_qualifies(tmp_path, capsys):
    state_path = tmp_path / "seen.json"
    hits_path = tmp_path / "hits.json"
    hit = make_hit(classification="FAIR")
    # Pre-seed state so this exact hit is treated as "already alerted, no
    # change". last_recap_at is recent too, so this isolates the cooldown
    # path from the weekly recap.
    state = {notify.item_key(hit): {
        "last_price": hit["price"], "last_alerted_price": hit["price"],
        "last_alerted_at": datetime.now(timezone.utc).isoformat(),
        "last_classification": "FAIR",
    }, notify.META_KEY: {"last_recap_at": datetime.now(timezone.utc).isoformat()}}
    state_path.write_text(json.dumps(state))

    alerting = notify.run_digest([hit], dry_run=True, state_path=state_path, hits_path=hits_path)

    assert alerting == []
    out = capsys.readouterr().out
    assert "silent run is valid" in out
    assert hits_path.exists()  # full set still written


def test_run_digest_writes_full_hit_set_regardless_of_alerting(tmp_path):
    state_path = tmp_path / "seen.json"
    hits_path = tmp_path / "hits.json"
    hits = [make_hit(classification="DEAL"), make_hit(classification="NOREF", url="https://x/2")]

    notify.run_digest(hits, dry_run=True, state_path=state_path, hits_path=hits_path)

    written = json.loads(hits_path.read_text())
    assert len(written) == 2


# --- an alert must not be silenced unless it was actually delivered ---------

def test_dry_run_does_not_consume_the_alert(tmp_path, monkeypatch):
    # A dry run that marked items alerted would silence the next real run
    # for the whole cooldown window -- the find would be lost silently.
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    hit = make_hit(classification="DEAL")

    notify.run_digest([hit], dry_run=True, state_path=state_path, hits_path=hits_path)

    # Nothing persisted, so a subsequent real run still sees it as new.
    sent = {}
    monkeypatch.setattr(notify, "send_email", lambda body, **kw: sent.setdefault("body", body))
    alerting = notify.run_digest([hit], dry_run=False, state_path=state_path, hits_path=hits_path)

    assert len(alerting) == 1
    assert "body" in sent, "the real run must still send after a dry run"


def test_failed_send_does_not_mark_the_item_alerted(tmp_path, monkeypatch):
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    hit = make_hit(classification="DEAL")

    def boom(body, **kw):
        raise notify.NotConfigured("no credentials")

    monkeypatch.setattr(notify, "send_email", boom)
    with pytest.raises(notify.NotConfigured):
        notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    # The hits are still recorded, and the item is NOT in cooldown.
    assert json.loads(hits_path.read_text())
    state = notify.load_state(state_path)
    assert state.get(notify.item_key(hit), {}).get("last_alerted_at") is None

    sent = {}
    monkeypatch.setattr(notify, "send_email", lambda body, **kw: sent.setdefault("body", body))
    alerting = notify.run_digest([hit], state_path=state_path, hits_path=hits_path)
    assert len(alerting) == 1 and "body" in sent


def test_successful_send_does_mark_the_item_alerted(tmp_path, monkeypatch):
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    hit = make_hit(classification="DEAL")
    monkeypatch.setattr(notify, "send_email", lambda body, **kw: None)

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)
    second = notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    assert second == [], "an delivered alert must go into cooldown"


def _many_hits(count):
    """`count` distinct alert-worthy hits, all new, all the same shape."""
    return [make_hit(classification="FAIR", price=50.0 + i,
                     title=f"Domaine Labet Cuvee {i}",
                     url=f"https://example-shop.test/products/labet-{i}")
            for i in range(count)]


def test_hits_beyond_the_row_cap_are_not_marked_alerted(tmp_path, monkeypatch):
    """The cap must not silence what it does not show.

    A live run had 79 alert-worthy hits, printed 40 and marked all 79. The
    other 39 were real finds that no email ever carried and that seen.json
    then kept quiet for 30 days.
    """
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    hits = _many_hits(notify.EMAIL_ROW_CAP + 6)
    monkeypatch.setattr(notify, "send_email", lambda body, **kw: None)

    alerting = notify.run_digest(hits, state_path=state_path,
                                 hits_path=hits_path)

    assert len(alerting) == notify.EMAIL_ROW_CAP
    state = notify.load_state(state_path)
    shown = {notify.item_key(h) for h in alerting}
    held = [h for h in hits if notify.item_key(h) not in shown]
    assert len(held) == 6
    for hit in held:
        assert notify.item_key(hit) not in state, \
            "a hit the email never showed must stay unmarked, not half-marked"


def test_the_held_back_hits_lead_the_next_digest(tmp_path, monkeypatch):
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    hits = _many_hits(notify.EMAIL_ROW_CAP + 6)
    monkeypatch.setattr(notify, "send_email", lambda body, **kw: None)

    first = notify.run_digest(hits, state_path=state_path, hits_path=hits_path)
    second = notify.run_digest(hits, state_path=state_path, hits_path=hits_path)

    assert len(second) == 6
    assert {notify.item_key(h) for h in second}.isdisjoint(
        {notify.item_key(h) for h in first})


def test_the_digest_says_how_many_it_held_back(tmp_path, monkeypatch):
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    sent = {}
    monkeypatch.setattr(notify, "send_email",
                        lambda body, **kw: sent.update(body=body, **kw))

    notify.run_digest(_many_hits(notify.EMAIL_ROW_CAP + 6),
                      state_path=state_path, hits_path=hits_path)

    assert "Held for the next digest" in sent["body"]
    assert "6 further new hit(s)" in sent["body"]
    assert "6 further new hit(s)" in sent["html"]


def test_holding_back_a_previously_alerted_hit_restores_its_old_entry(tmp_path,
                                                                     monkeypatch):
    """Not every held hit is new -- one can be re-alerting on a price drop.

    Restoring its previous entry leaves the old alert stamp in place, so it
    re-alerts on the same drop next run instead of being recorded as reported.
    """
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    monkeypatch.setattr(notify, "send_email", lambda body, **kw: None)

    # One older hit, alerted a while ago at a high price, now much cheaper --
    # a drop, so it alerts again. It is ordered last by being the only NOREF.
    dropped = make_hit(classification="NOREF", price=40.0,
                       expected_price=None,
                       url="https://example-shop.test/products/dropped")
    before = {notify.item_key(dropped): {
        "last_price": 100.0, "last_alerted_price": 100.0,
        "last_alerted_at": (datetime.now(timezone.utc)
                            - timedelta(days=2)).isoformat(),
        "last_classification": "NOREF"}}
    state_path.write_text(json.dumps(before))

    notify.run_digest(_many_hits(notify.EMAIL_ROW_CAP) + [dropped],
                      state_path=state_path, hits_path=hits_path)

    after = notify.load_state(state_path)
    assert after[notify.item_key(dropped)] == before[notify.item_key(dropped)]


def test_missing_credentials_raise_a_legible_error(monkeypatch):
    for key in ("GMAIL_SENDER", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(notify.NotConfigured) as excinfo:
        notify.send_email("body")

    message = str(excinfo.value)
    assert "GMAIL_SENDER" in message and "hits.json" in message


# --- the weekly recap: silence must not be ambiguous ---------------------------
#
# Even with news breaking the cooldown, a week can pass with nothing new and
# nothing cheaper. That run is correct to say nothing -- but from the inbox it
# is indistinguishable from expired credentials, a dead adapter or a workflow
# that stopped firing. So: if nothing has been emailed for RECAP_DAYS and
# there are hits, send what we can currently see.


class Recorder:
    """Stands in for SMTP, keeping subjects as well as bodies."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def __call__(self, body, subject=None, html=None):
        if self.fail:
            raise notify.NotConfigured("no credentials")
        self.calls.append((subject, body))

    @property
    def bodies(self):
        return [b for _, b in self.calls]


def days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def seeded_state(hit, alerted_days_ago=1, recap_days_ago=None):
    """State where `hit` was alerted and is now in cooldown with no news."""
    state = {notify.item_key(hit): {
        "last_price": hit["price"],
        "last_alerted_price": hit["price"],
        "last_alerted_at": days_ago(alerted_days_ago),
        "last_classification": hit["classification"],
    }}
    if recap_days_ago is not None:
        state[notify.META_KEY] = {"last_recap_at": days_ago(recap_days_ago)}
    return state


def test_a_week_without_an_email_produces_a_recap(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(
        seeded_state(hit, alerted_days_ago=8, recap_days_ago=8)))

    alerting = notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    assert alerting == [], "the recap is not an alert"
    assert len(send.calls) == 1
    assert hit["cuvee"] in send.bodies[0]


def test_a_recap_is_labelled_and_has_its_own_subject(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(
        seeded_state(hit, alerted_days_ago=8, recap_days_ago=8)))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    subject, body = send.calls[0]
    assert notify.RECAP_SUBJECT in subject
    assert notify.DIGEST_SUBJECT not in subject
    assert "recap" in body.lower()


def test_no_recap_before_the_week_is_up(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(
        seeded_state(hit, alerted_days_ago=2, recap_days_ago=2)))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    assert send.calls == [], "an hourly recap would be worse than silence"


def test_a_recap_does_not_put_anything_into_cooldown(tmp_path, monkeypatch):
    """Marking an item alerted is what silences it for 30 days. A recap is
    not a find, so it must leave every item's alert record untouched."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    before = seeded_state(hit, alerted_days_ago=8, recap_days_ago=8)
    state_path.write_text(json.dumps(before))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    entry = notify.load_state(state_path)[notify.item_key(hit)]
    assert entry["last_alerted_at"] == before[notify.item_key(hit)]["last_alerted_at"]
    assert entry["last_alerted_price"] == hit["price"]


def test_a_recap_resets_its_own_clock(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(
        seeded_state(hit, alerted_days_ago=8, recap_days_ago=8)))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)
    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    assert len(send.calls) == 1, "the recap repeated itself on the next run"


def test_a_real_digest_also_resets_the_weekly_clock(tmp_path, monkeypatch):
    """The promise is 'you hear from it at least weekly', not 'you get an
    extra email weekly'."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps({notify.META_KEY: {"last_recap_at": days_ago(30)}}))

    notify.run_digest([make_hit(classification="DEAL")],
                      state_path=state_path, hits_path=hits_path)

    assert len(send.calls) == 1, "a digest and a recap went out for the same run"
    assert notify.DIGEST_SUBJECT in send.calls[0][0]
    meta = notify.load_state(state_path)[notify.META_KEY]
    assert (datetime.now(timezone.utc) - notify._parse_iso(meta["last_recap_at"])).days == 0


def test_nothing_to_report_stays_silent_however_long_it_has_been(tmp_path, monkeypatch):
    """The weekly clock is not a heartbeat for the workflow. With no hits
    there is nothing to recap."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps({notify.META_KEY: {"last_recap_at": days_ago(99)}}))

    notify.run_digest([], state_path=state_path, hits_path=hits_path)

    assert send.calls == []


def test_a_first_ever_run_with_no_state_does_not_double_email(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"

    notify.run_digest([make_hit(classification="DEAL")],
                      state_path=state_path, hits_path=hits_path)

    assert len(send.calls) == 1


def test_a_dry_run_recap_sends_nothing_and_persists_nothing(tmp_path, monkeypatch, capsys):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state = seeded_state(hit, alerted_days_ago=8, recap_days_ago=8)
    state_path.write_text(json.dumps(state))

    notify.run_digest([hit], dry_run=True, state_path=state_path, hits_path=hits_path)

    assert send.calls == []
    assert notify.load_state(state_path) == state, "a dry run consumed the recap"
    assert "recap" in capsys.readouterr().out.lower()


def test_a_failed_recap_send_does_not_reset_the_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "send_email", Recorder(fail=True))
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state = seeded_state(hit, alerted_days_ago=8, recap_days_ago=8)
    state_path.write_text(json.dumps(state))

    with pytest.raises(notify.NotConfigured):
        notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    assert notify.load_state(state_path) == state


def test_the_recap_carries_the_run_notes(tmp_path, monkeypatch):
    """Drift and missing producers are exactly what someone reads a recap
    for."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(
        seeded_state(hit, alerted_days_ago=8, recap_days_ago=8)))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path,
                      notes={"Shops that returned nothing": ["mareehaute"]})

    assert "mareehaute" in send.bodies[0]


def test_the_meta_key_can_never_collide_with_an_item():
    """seen.json is keyed by sha256 hex, so a reserved non-hex key is safe."""
    assert not all(c in "0123456789abcdef" for c in notify.META_KEY)
    key = notify.item_key(make_hit())
    assert len(key) == 64 and key != notify.META_KEY


def test_select_alerts_never_writes_the_meta_key():
    _, state = notify.select_alerts([make_hit()], state={})
    assert notify.META_KEY not in state


def test_state_that_predates_the_recap_gets_one_promptly(tmp_path, monkeypatch):
    """The state carried in the Actions cache has no recap clock, so the
    first run after this lands has nothing to measure a week from. Sending
    the recap is the right reading: that state is exactly the situation the
    recap exists for -- a shelf full of hits, all in cooldown, silent for
    days."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(seeded_state(hit, alerted_days_ago=3)))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    assert len(send.calls) == 1
    assert notify.RECAP_SUBJECT in send.calls[0][0]


def test_a_dry_run_with_nothing_to_say_writes_no_state(tmp_path, monkeypatch):
    """A dry run leaves no trace on every path, not just the sending one.
    The silent branch used to refresh last_price even under DRY_RUN."""
    monkeypatch.setattr(notify, "send_email", Recorder())
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state = seeded_state(hit, alerted_days_ago=1, recap_days_ago=1)
    state[notify.item_key(hit)]["last_price"] = 999.0   # a save would fix this
    state_path.write_text(json.dumps(state))

    alerting = notify.run_digest([hit], dry_run=True, state_path=state_path,
                                 hits_path=hits_path)

    assert alerting == []
    assert notify.load_state(state_path) == state


# --- a run the owner asked for must always answer -------------------------------
#
# The failure this fixes, from the log of a button press:
#
#   51 raw producer match(es) this run.
#   No newly alert-worthy hits this run (cooldown or no change) -- silent run
#   is valid.
#
# Correct, and useless. The hourly schedule should stay quiet when there is no
# news, but a human who presses "Run scraper" and gets nothing back cannot
# tell that from expired credentials, and has twice now concluded the thing
# had stopped working.


def all_quiet(hit):
    """Everything alerted recently, and the weekly recap not due either --
    the exact state in which a button press currently says nothing."""
    return seeded_state(hit, alerted_days_ago=1, recap_days_ago=1)


def test_a_run_the_owner_asked_for_always_reports(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(all_quiet(hit)))

    alerting = notify.run_digest([hit], state_path=state_path,
                                 hits_path=hits_path, force=True)

    assert alerting == [], "a forced report is not a set of new finds"
    assert len(send.calls) == 1
    assert hit["cuvee"] in send.bodies[0]


def test_the_same_state_stays_silent_on_a_scheduled_run(tmp_path, monkeypatch):
    """The other half: hourly runs must not start emailing every hour."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(all_quiet(hit)))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    assert send.calls == []


def test_a_forced_report_marks_nothing_alerted(tmp_path, monkeypatch):
    """Same rule as the recap: it is not a find, and marking one would
    silence a real price drop for 30 days."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    before = all_quiet(hit)
    state_path.write_text(json.dumps(before))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path, force=True)

    entry = notify.load_state(state_path)[notify.item_key(hit)]
    assert entry["last_alerted_at"] == before[notify.item_key(hit)]["last_alerted_at"]


def test_a_forced_report_answers_even_with_nothing_to_show(tmp_path, monkeypatch):
    """The one place silence is worse than an empty table. A scheduled run
    with no hits still sends nothing; a run someone asked for says so."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"

    notify.run_digest([], state_path=state_path, hits_path=hits_path, force=True,
                      notes={"Watched but found nowhere": ["Ganevat"]})

    assert len(send.calls) == 1
    assert "Ganevat" in send.bodies[0]


def test_a_forced_report_says_which_kind_of_email_it_is(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(all_quiet(hit)))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path, force=True)

    subject, body = send.calls[0]
    assert notify.ONDEMAND_SUBJECT in subject
    assert notify.DIGEST_SUBJECT not in subject
    assert notify.RECAP_SUBJECT not in subject
    assert "asked for" in body.lower() or "on demand" in body.lower()


def test_a_forced_run_with_real_news_sends_the_digest_not_two_emails(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"

    notify.run_digest([make_hit(classification="DEAL")], state_path=state_path,
                      hits_path=hits_path, force=True)

    assert len(send.calls) == 1
    assert notify.DIGEST_SUBJECT in send.calls[0][0]


def test_a_forced_report_resets_the_weekly_clock(tmp_path, monkeypatch):
    """It is an email the owner received, so the recap should not follow it
    a day later."""
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state_path.write_text(json.dumps(
        seeded_state(hit, alerted_days_ago=1, recap_days_ago=30)))

    notify.run_digest([hit], state_path=state_path, hits_path=hits_path, force=True)
    notify.run_digest([hit], state_path=state_path, hits_path=hits_path)

    assert len(send.calls) == 1


def test_a_forced_dry_run_still_sends_nothing(tmp_path, monkeypatch):
    send = Recorder()
    monkeypatch.setattr(notify, "send_email", send)
    hit = make_hit(classification="FAIR")
    state_path, hits_path = tmp_path / "seen.json", tmp_path / "hits.json"
    state = all_quiet(hit)
    state_path.write_text(json.dumps(state))

    notify.run_digest([hit], dry_run=True, state_path=state_path,
                      hits_path=hits_path, force=True)

    assert send.calls == []
    assert notify.load_state(state_path) == state


# --- the credentials as a human actually pastes them --------------------------

class FakeSMTP:
    """Records what was handed to SMTP AUTH, which is the thing that fails."""
    last = None

    def __init__(self, host, port):
        FakeSMTP.last = {"host": host, "port": port}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        FakeSMTP.last.update(user=user, password=password)

    def sendmail(self, sender, recipients, message):
        FakeSMTP.last.update(sender=sender, recipients=recipients, message=message)


def test_an_app_password_pasted_as_google_displays_it_still_authenticates(monkeypatch):
    """Google shows app passwords as four groups of four. SMTP AUTH sends
    whatever string it is given, so pasting it as displayed fails with what
    looks like a wrong password -- a bad half hour for anyone setting this up
    on a phone. No app password contains a space, so stripping cannot break a
    correct one."""
    monkeypatch.setenv("GMAIL_SENDER", "someone@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setenv("NOTIFY_EMAIL", "someone@example.com")
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)

    notify.send_email("body", subject="subj")
    assert FakeSMTP.last["password"] == "abcdefghijklmnop"


def test_a_newline_or_stray_space_around_any_credential_is_ignored(monkeypatch):
    """A secrets box on a phone collects trailing whitespace easily, and an
    address with a newline in it is not an address."""
    monkeypatch.setenv("GMAIL_SENDER", "  someone@gmail.com\n")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "\nabcdefghijklmnop  ")
    monkeypatch.setenv("NOTIFY_EMAIL", " someone@example.com\n")
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)

    notify.send_email("body", subject="subj")
    assert FakeSMTP.last["user"] == "someone@gmail.com"
    assert FakeSMTP.last["password"] == "abcdefghijklmnop"
    assert FakeSMTP.last["recipients"] == ["someone@example.com"]


def test_the_digest_still_goes_to_gmail_over_tls(monkeypatch):
    monkeypatch.setenv("GMAIL_SENDER", "someone@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcdefghijklmnop")
    monkeypatch.setenv("NOTIFY_EMAIL", "someone@example.com")
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)

    notify.send_email("body", subject="subj")
    assert FakeSMTP.last["host"] == "smtp.gmail.com"
    assert FakeSMTP.last["port"] == 465


# --- the HTML part and the subject line ---------------------------------------
#
# The mail was plain text with pipe-delimited rows whose last column was the
# URL, which on a phone wrapped one hit across four unreadable lines; and the
# subject was a constant, so the inbox could not tell a run with three deals
# from a run with none. Both halves are covered here, plus the two properties
# that must survive: the text part is still sent, and it is still first.

def test_the_message_carries_both_a_text_and_an_html_part():
    """multipart/alternative, text first. A text-only client must still get
    the plain body this project has always sent, never a wall of markup."""
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def sendmail(self, sender, to, raw): sent["raw"] = raw

    import email as email_mod
    notify.os.environ.update(GMAIL_SENDER="a@b.test",
                             GMAIL_APP_PASSWORD="pw", NOTIFY_EMAIL="c@d.test")
    real = notify.smtplib.SMTP_SSL
    notify.smtplib.SMTP_SSL = FakeSMTP
    try:
        notify.send_email("the plain body", subject="s", html="<p>markup</p>")
    finally:
        notify.smtplib.SMTP_SSL = real

    msg = email_mod.message_from_string(sent["raw"])
    assert msg.get_content_type() == "multipart/alternative"
    parts = msg.get_payload()
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"], \
        "text must come first -- the order is the client's preference order"
    assert "the plain body" in parts[0].get_payload()
    assert "markup" in parts[1].get_payload()


def test_a_message_without_html_stays_a_plain_text_message():
    """Passing no html must leave the message exactly what it always was."""
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def sendmail(self, sender, to, raw): sent["raw"] = raw

    import email as email_mod
    notify.os.environ.update(GMAIL_SENDER="a@b.test",
                             GMAIL_APP_PASSWORD="pw", NOTIFY_EMAIL="c@d.test")
    real = notify.smtplib.SMTP_SSL
    notify.smtplib.SMTP_SSL = FakeSMTP
    try:
        notify.send_email("just text", subject="s")
    finally:
        notify.smtplib.SMTP_SSL = real

    msg = email_mod.message_from_string(sent["raw"])
    assert msg.get_content_type() == "text/plain"
    assert not msg.is_multipart()


def test_the_subject_names_the_count_and_the_leading_wine():
    subject = notify.subject_for(notify.DIGEST_SUBJECT,
                                [make_hit(classification="DEAL")])
    assert notify.DIGEST_SUBJECT in subject, "the kind must stay readable"
    assert "1 deal" in subject and "1 deals" not in subject
    assert "Labet" in subject


def test_an_empty_forced_report_says_so_in_the_subject():
    """The one case where an empty mail is the point: it must not look like a
    delivery failure from the notification alone."""
    subject = notify.subject_for(notify.ONDEMAND_SUBJECT, [])
    assert notify.ONDEMAND_SUBJECT in subject
    assert "nothing matched" in subject


def test_the_subject_leads_with_the_same_hit_the_body_does():
    """A subject naming one wine and a body opening with another reads as two
    different runs."""
    hits = [make_hit(classification="HIGH", producer="Zzz High"),
            make_hit(classification="DEAL", producer="Aaa Deal")]
    subject = notify.subject_for(notify.DIGEST_SUBJECT, hits)
    body = notify.build_digest_body(hits)
    assert "Aaa Deal" in subject
    first_row = [l for l in body.splitlines() if "|" in l and "STATUS" not in l][0]
    assert "Aaa Deal" in first_row


def test_the_html_links_the_wine_and_drops_the_url_column():
    hit = make_hit(classification="DEAL")
    html = notify.build_digest_html([hit])
    assert f'href="{hit["url"]}"' in html, "the wine itself must be the link"
    assert "Labet" in html and "Cotes du Jura Chardonnay" in html
    # The plain text keeps the URL as a column; the HTML must not repeat it as
    # bare text, which is what wrapped across four lines on a phone.
    assert html.count(hit["url"]) == 1


def test_the_html_states_the_gap_between_price_and_reference():
    """Subtracting two numbers is not the reader's job."""
    html = notify.build_digest_html(
        [make_hit(classification="DEAL", price=60.0, expected_price=80.0)])
    assert "EUR 60" in html and "EUR 80" in html
    assert "-25%" in html, "the gap must be stated, not left to be worked out"


def test_the_html_escapes_shop_supplied_text():
    """Titles come from shop pages, which are untrusted input, and this one
    goes into an HTML document."""
    html = notify.build_digest_html(
        [make_hit(cuvee='Evil <script>alert(1)</script>', classification="DEAL")])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_the_html_carries_the_diagnostics_too():
    """Coverage and notes are how a silent failure reaches a person; they must
    survive into the HTML half, not only the text one."""
    html = notify.build_digest_html(
        [make_hit(classification="DEAL")],
        notes={"Watched but found nowhere": ["Ganevat"]},
        tables={"Shop coverage": ["shop | ok | 1"]})
    assert "Watched but found nowhere" in html and "Ganevat" in html
    assert "Shop coverage" in html and "shop | ok | 1" in html


def test_a_silenced_line_stays_out_of_the_html_as_well():
    """NOALERT rows are kept out of every email body. The HTML part is a new
    body and must obey the same rule."""
    sent = {}
    real = notify.send_email
    notify.send_email = lambda body, subject=None, html=None: sent.update(
        body=body, html=html)
    try:
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            notify.run_digest(
                [make_hit(classification="DEAL", producer="Shown"),
                 make_hit(classification="NOALERT", producer="Silenced",
                          alertable=False)],
                state_path=d / "seen.json", hits_path=d / "hits.json")
    finally:
        notify.send_email = real
    assert "Shown" in sent["html"]
    assert "Silenced" not in sent["html"]
