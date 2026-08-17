"""Notification hygiene: one digest email per run, sha256-keyed cooldown
state in seen.json, and the full evaluated hit set in hits.json for the
workflow artifact.

Never one email per hit. A run where nothing newly qualifies sends nothing
and exits 0 -- a silent run is a valid run, not a failure. But a whole week
of them is not distinguishable from a broken one from the inbox, so after
RECAP_DAYS of quiet the run sends a recap of what it can currently see.
"""
import hashlib
import html as html_escape
import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

STATE_PATH = Path(os.environ.get("SEEN_STATE_PATH", Path(__file__).parent / "seen.json"))
HITS_PATH = Path(os.environ.get("HITS_OUTPUT_PATH", Path(__file__).parent / "hits.json"))

COOLDOWN_DAYS = 30
PRICE_DROP_THRESHOLD = 0.10
EMAIL_ROW_CAP = 40
SECTION_ORDER = ["DEAL", "FAIR", "NOREF", "HIGH", "NOALERT"]

# How long the tracker may stay silent before it says so unprompted. A run
# with no news is right to send nothing, but from the inbox that is
# indistinguishable from expired credentials or a dead adapter.
RECAP_DAYS = 7
DIGEST_SUBJECT = "Wine tracker digest"
RECAP_SUBJECT = "Wine tracker weekly recap"
# A run a human pressed the button for. It answers whatever the state of the
# cooldown, because silence from a run you asked for is indistinguishable
# from a broken one -- and twice now has been read as exactly that.
ONDEMAND_SUBJECT = "Wine tracker report (you asked)"
# seen.json is keyed by sha256 hex, so a non-hex key cannot collide with an
# item, and select_alerts only ever writes keys it computed itself.
META_KEY = "_meta"


def item_key(hit):
    """sha256(shop + product_url + variant) -- stable identity for a single
    listing, distinct across shops/variants even if the same wine appears
    at more than one shop or in more than one bottle size."""
    raw = f"{hit.get('shop', '')}|{hit.get('url', '')}|{hit.get('variant_title', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_state(path=None):
    path = path or STATE_PATH
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state, path=None):
    path = path or STATE_PATH
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _parse_iso(ts):
    return datetime.fromisoformat(ts)


def should_alert(hit, prev, now):
    """new item -> always. A >10% drop since the last alert -> always, too:
    the cooldown is there to stop the digest repeating itself, and a price
    drop is not a repeat. Otherwise the 30-day cooldown blocks re-alerting,
    and past it only a classification improving to DEAL qualifies.

    The drop rule ignoring the cooldown is deliberate and was once the other
    way round. An *unchanged* item never re-alerts either way -- the
    post-cooldown branch also requires news -- so all the 30-day gate ever
    did to a price drop was delay it by up to a month, which is how a live
    tracker goes quiet for weeks while a wine it already reported halves in
    price. Nothing runs away as a result: the comparison is against
    `last_alerted_price`, which every alert resets, so a decline alerts once
    per further -10% step and a round trip alerts not at all.

    Classification stays behind the cooldown on purpose. It is derived from
    the observed market pool, which moves every hour as other shops are
    crawled, so DEAL -> FAIR -> DEAL flapping is realistic in a way a price
    round trip is not."""
    # A line configured never to alert. The hit is still in hits.json and in
    # the digest table -- Ganevat's negoce-from-outside-the-Jura is a real
    # listing worth seeing, it is just never news at any price, because its
    # price says nothing about the domaine bottles we are actually hunting.
    if hit.get("alertable") is False:
        return False

    if prev is None:
        return True

    last_alerted_price = prev.get("last_alerted_price")
    price = hit.get("price")
    price_dropped = (
        last_alerted_price is not None
        and price is not None
        and price <= last_alerted_price * (1 - PRICE_DROP_THRESHOLD)
    )
    if price_dropped:
        return True

    last_alerted_at = prev.get("last_alerted_at")
    if last_alerted_at and (now - _parse_iso(last_alerted_at)).days < COOLDOWN_DAYS:
        return False

    return (hit.get("classification") == "DEAL"
            and prev.get("last_classification") != "DEAL")


def _update_state(state, hit, key, alerted, now):
    entry = dict(state.get(key, {}))
    entry["last_price"] = hit.get("price")
    if alerted:
        entry["last_alerted_price"] = hit.get("price")
        entry["last_alerted_at"] = now.isoformat()
        entry["last_classification"] = hit.get("classification")
    state[key] = entry
    return state


def select_alerts(hits, state=None, now=None):
    """Decide which hits are alert-worthy this run, and return the updated
    state. last_price is refreshed for every hit seen, alerted or not, so
    future price-drop comparisons stay accurate."""
    now = now or datetime.now(timezone.utc)
    state = {} if state is None else dict(state)
    alerting = []
    for hit in hits:
        key = item_key(hit)
        prev = state.get(key)
        alert = should_alert(hit, prev, now)
        if alert:
            alerting.append(hit)
        state = _update_state(state, hit, key, alert, now)
    return alerting, state


def format_row(hit):
    status = hit.get("classification", "NOREF") + ("*" if hit.get("caveat") else "")
    price = f"EUR {hit['price']:.0f}" if hit.get("price") is not None else "EUR ?"
    ref = f"EUR {hit['expected_price']:.0f}" if hit.get("expected_price") is not None else "EUR ?"
    size = hit.get("size_label") or f"{hit.get('size_ml', 750)}ml"
    cuvee = hit.get("cuvee") or hit.get("title", "")
    producer = hit.get("producer", "")
    # The alias that fired is the whole diagnosis for a misattribution --
    # three estates were reported under the wrong producer, each caught only
    # by someone recognising the name and opening the shop.
    alias = hit.get("matched_alias")
    if alias:
        producer = f"{producer} [{alias}]"
    # Where the reference came from is the difference between "cheaper than
    # three other shops" and "cheaper than a number someone guessed once".
    basis = hit.get("reference_basis") or "no reference"
    return (f"{status:<5} | {producer} | {cuvee} | {size} | {price} | {ref} | "
            f"{basis} | {hit.get('url', '')}")


def recap_due(state, now, every_days=RECAP_DAYS):
    """True when nothing has been emailed for `every_days`. The clock is
    reset by any digest, not only by a recap -- the promise is "you hear from
    it at least weekly", not "you get an extra email weekly"."""
    last = (state.get(META_KEY) or {}).get("last_recap_at")
    if not last:
        return True
    return (now - _parse_iso(last)).total_seconds() >= every_days * 86400


def _stamp_recap(state, now):
    meta = dict(state.get(META_KEY) or {})
    meta["last_recap_at"] = now.isoformat()
    state[META_KEY] = meta
    return state


def order_hits(hits):
    """Strongest news first, by classification.

    Shared by the body, the HTML part and the subject line so all three lead
    with the same hit -- a subject naming one wine and a body opening with
    another reads as two different runs.
    """
    ordered = []
    for section in SECTION_ORDER:
        ordered.extend(h for h in hits if h.get("classification") == section)
    return ordered


def _shorten(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def subject_for(base, hits):
    """Put the news in the subject line, and put it first.

    A constant subject makes every run look alike in the inbox, so triage
    means opening the mail. The news leads because a phone shows roughly the
    first 35 characters of a subject and nothing more: with the kind in front,
    "Wine tracker digest: 3 deals -" fills that on its own and the wine never
    appears. The kind still travels, at the end, where the three sorts of mail
    stay distinguishable without spending the space that matters.

    An empty forced report says "nothing matched" outright -- that mail exists
    precisely so silence cannot be mistaken for a delivery failure.
    """
    if not hits:
        return f"nothing matched · {base}"
    lead = order_hits(hits)[0]
    deals = sum(1 for h in hits if h.get("classification") == "DEAL")
    head = _plural(deals, "deal") if deals else _plural(len(hits), "hit")
    wine = " ".join(x for x in (
        lead.get("producer", ""),
        _shorten(lead.get("cuvee") or lead.get("title") or "", 30),
    ) if x)
    price = f" EUR {lead['price']:.0f}" if lead.get("price") is not None else ""
    rest = f" +{len(hits) - 1} more" if len(hits) > 1 else ""
    return f"{head} — {wine}{price}{rest} · {base}"


def build_digest_body(alerting_hits, notes=None, recap=False, on_demand=False,
                      tables=None):
    ordered = order_hits(alerting_hits)
    shown = ordered[:EMAIL_ROW_CAP]

    lines = []
    if on_demand:
        lines += [
            "You asked for this run, so here is everything currently matched, "
            "new or not. Nothing below has been marked as alerted, so a real "
            "find or price drop will still reach you on its own.",
            "",
        ]
    elif recap:
        lines += [
            f"Weekly recap. Nothing new and nothing more than "
            f"{PRICE_DROP_THRESHOLD:.0%} cheaper in the last {RECAP_DAYS} days, so "
            f"this is everything currently matched rather than a set of fresh "
            f"finds -- and confirmation that the tracker is still running.",
            "",
        ]
    lines += ["STATUS | Producer | Cuvee | Size | Price | Ref | Basis | Link", ""]
    has_caveat = False
    for section in SECTION_ORDER:
        items = [h for h in shown if h.get("classification") == section]
        if not items:
            continue
        heading = "Flagged as overpriced" if section == "HIGH" else section
        lines.append(heading)
        for hit in items:
            lines.append(format_row(hit))
            has_caveat = has_caveat or bool(hit.get("caveat"))
        lines.append("")

    if len(ordered) > EMAIL_ROW_CAP:
        kind = "matched" if (recap or on_demand) else "alert-worthy"
        lines.append(f"... {len(ordered) - EMAIL_ROW_CAP} more {kind} hit(s) omitted; see hits.json")

    if has_caveat:
        lines.append("* reference unverified or size/tier confidence low -- treat with caution")

    # Notes are how a silent failure reaches the person rather than only the
    # run log. Rendered only when non-empty, so a clean run gains nothing.
    # A table is rendered a row per line, unlike a note, which is a
    # comma-joined list. Same mechanism otherwise: nothing renders when empty.
    for heading, rows in (tables or {}).items():
        if rows:
            lines.append("")
            lines.append(heading)
            lines.extend(rows)

    for heading, names in (notes or {}).items():
        if names:
            lines.append("")
            lines.append(f"{heading} ({len(names)}): {', '.join(names)}")

    return "\n".join(lines).rstrip() + "\n"


def _esc(text):
    return html_escape.escape(str(text or ""))


# Inline styles only, and no image, script or webfont. Mail clients strip
# <style> blocks unpredictably and remote content is what puts a new sender in
# a junk folder -- which this project has already spent an evening escaping.
_H = {
    "wrap": "font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#241c22;max-width:640px",
    "lede": "margin:0 0 18px;color:#6b5f66;font-size:14px",
    "sect": "margin:22px 0 8px;font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#8c2f4a",
    "row": "padding:10px 0;border-top:1px solid #ddd5dc",
    "name": "font-weight:600;font-size:15px;color:#241c22;text-decoration:none",
    "meta": "margin:3px 0 0;font-size:13px;color:#6b5f66",
    "note": "margin:2px 0 0;font-size:12px;color:#918790",
    "pre": "font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;background:#f2eff2;padding:10px;overflow-x:auto;white-space:pre",
    "foot": "margin-top:26px;padding-top:10px;border-top:1px solid #ddd5dc;font-size:12px;color:#918790",
}


def _hit_html(hit):
    """One hit as a block, ordered by the decision it supports.

    The wine is the link, so the URL stops being a column that wraps across
    four lines on a phone; the price sits next to the reference with the gap
    already worked out, because subtracting two numbers is not the reader's
    job.
    """
    producer = _esc(hit.get("producer", ""))
    alias = hit.get("matched_alias")
    # The alias that fired is the whole diagnosis for a misattribution.
    label = f"{producer} [{_esc(alias)}]" if alias else producer
    cuvee = _esc(_shorten(hit.get("cuvee") or hit.get("title") or "", 80))
    url = _esc(hit.get("url", ""))
    title = f"{label} — {cuvee}" if cuvee else label
    head = (f'<a href="{url}" style="{_H["name"]}">{title}</a>'
            if url else f'<span style="{_H["name"]}">{title}</span>')

    price = hit.get("price")
    ref = hit.get("expected_price")
    money = f"EUR {price:.0f}" if price is not None else "price unknown"
    if price is not None and ref:
        pct = price / ref - 1
        money += f" vs EUR {ref:.0f} ref ({pct:+.0%})"
    elif ref:
        money += f" vs EUR {ref:.0f} ref"
    size = _esc(hit.get("size_label") or f"{hit.get('size_ml', 750)}ml")
    shop = _esc(hit.get("shop", ""))
    meta = " · ".join(x for x in (_esc(money), size, shop) if x)

    lines = [f'<div style="{_H["row"]}">{head}',
             f'<p style="{_H["meta"]}">{meta}</p>']
    basis = hit.get("reference_basis")
    if basis:
        lines.append(f'<p style="{_H["note"]}">{_esc(basis)}</p>')
    if hit.get("caveat"):
        lines.append(f'<p style="{_H["note"]}">Reference unverified, or size/tier '
                     f'confidence low — treat with caution.</p>')
    lines.append("</div>")
    return "".join(lines)


def build_digest_html(alerting_hits, notes=None, recap=False, on_demand=False,
                      tables=None):
    """The HTML half of the mail. The plain text half stays authoritative.

    Sent alongside the text part rather than instead of it: a text client
    still gets the version this project has always sent, and the diagnostics
    keep their alignment by staying in a <pre>.
    """
    ordered = order_hits(alerting_hits)
    shown = ordered[:EMAIL_ROW_CAP]
    deals = sum(1 for h in alerting_hits if h.get("classification") == "DEAL")

    out = [f'<div style="{_H["wrap"]}">']
    if on_demand:
        lede = ("You asked for this run, so this is everything currently matched, "
                "new or not. Nothing here has been marked as alerted.")
    elif recap:
        lede = (f"Weekly recap: nothing new and nothing more than "
                f"{PRICE_DROP_THRESHOLD:.0%} cheaper in {RECAP_DAYS} days. "
                f"Everything currently matched, and confirmation the tracker runs.")
    else:
        lede = (f"{deals} deal(s) among {len(alerting_hits)} alert-worthy hit(s).")
    out.append(f'<p style="{_H["lede"]}">{_esc(lede)}</p>')

    if not shown:
        out.append(f'<p style="{_H["meta"]}">Nothing matched at all this run. '
                   f'That is a real answer, not a failure — but if it repeats, '
                   f'check the coverage table below.</p>')

    for section in SECTION_ORDER:
        items = [h for h in shown if h.get("classification") == section]
        if not items:
            continue
        heading = "Flagged as overpriced" if section == "HIGH" else section
        out.append(f'<div style="{_H["sect"]}">{_esc(heading)} ({len(items)})</div>')
        out.extend(_hit_html(h) for h in items)

    if len(ordered) > EMAIL_ROW_CAP:
        kind = "matched" if (recap or on_demand) else "alert-worthy"
        out.append(f'<p style="{_H["note"]}">… {len(ordered) - EMAIL_ROW_CAP} more '
                   f'{kind} hit(s) omitted; see hits.json in the run artifact.</p>')

    # Diagnostics last and quiet: they are not why the mail was opened, but
    # they are how a silent failure reaches a person at all.
    for heading, rows in (tables or {}).items():
        if rows:
            out.append(f'<div style="{_H["sect"]}">{_esc(heading)}</div>')
            out.append(f'<div style="{_H["pre"]}">'
                       + "\n".join(_esc(r) for r in rows) + "</div>")
    for heading, names in (notes or {}).items():
        if names:
            out.append(f'<p style="{_H["foot"]}"><b>{_esc(heading)}</b> '
                       f'({len(names)}): {_esc(", ".join(names))}</p>')

    out.append("</div>")
    return "".join(out)


class NotConfigured(Exception):
    """SMTP credentials are missing, so the digest cannot be delivered."""


def send_email(body, subject=DIGEST_SUBJECT, html=None):
    missing = [k for k in ("GMAIL_SENDER", "GMAIL_APP_PASSWORD", "NOTIFY_EMAIL") if not os.environ.get(k)]
    if missing:
        raise NotConfigured(
            f"Cannot send the digest: {', '.join(missing)} not set. "
            "Add them under Settings > Secrets and variables > Actions. "
            "The hits are still in hits.json, and nothing has been marked "
            "as alerted, so they will be re-reported on the next run."
        )
    sender = os.environ["GMAIL_SENDER"].strip()
    # Google shows an app password as four groups of four -- "abcd efgh ijkl
    # mnop" -- and SMTP AUTH sends whatever string it is given, so a password
    # pasted as displayed fails to authenticate for a reason that looks like a
    # wrong password. No app password contains a space, so removing whitespace
    # cannot break a correct one. Same for a stray newline on any of the three,
    # which is easy to acquire pasting into a secrets box on a phone.
    password = "".join(os.environ["GMAIL_APP_PASSWORD"].split())
    recipient = os.environ["NOTIFY_EMAIL"].strip()
    # multipart/alternative, text part first: the order is the preference
    # order, so a text-only client shows the plain body this project has
    # always sent and never a wall of markup. Without an html part the message
    # stays exactly what it was.
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html, "html"))
    else:
        msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())


def write_hits_json(all_hits, path=None):
    path = path or HITS_PATH
    path.write_text(json.dumps(all_hits, indent=2, sort_keys=True, default=str))


def run_digest(all_hits, dry_run=False, state_path=None, hits_path=None,
               notes=None, now=None, force=False, tables=None):
    """Full pipeline: decide alerts, write the full hit set to hits.json,
    send at most one email, and only then persist the cooldown state.
    Returns the list of alerting hits.

    Ordering matters. Marking an item "alerted" is what silences it for the
    next 30 days, so it must happen only once the email has actually gone
    out. Saving first meant a dry run consumed the alert -- the following
    real run would find it in cooldown and say nothing -- and a failed send
    (missing SMTP credentials, Gmail down) discarded the find entirely.
    Both are silent misses, which is the failure this scraper exists to
    avoid.

    With nothing to alert on, one of three things happens. Usually: silence,
    which is a valid run. If nothing has been emailed for RECAP_DAYS, the run
    sends a recap of everything currently matched instead -- otherwise a
    correct week of quiet looks exactly like a broken one. And if `force` is
    set -- a run a human explicitly started -- it always reports, cooldown or
    no cooldown, even when there is nothing at all to show. That last case is
    the one place an empty table beats silence: the alternative is a button
    that looks broken every time there is no news.

    Neither the recap nor the forced report marks anything as alerted. They
    are not finds, and marking one would silence a real price drop for 30
    days.
    """
    now = now or datetime.now(timezone.utc)
    state = load_state(state_path)
    alerting, updated_state = select_alerts(all_hits, state, now)
    write_hits_json(all_hits, hits_path)

    # A line configured never to alert stays out of every email body, not just
    # the alert list. A recap of "everything currently matched" would otherwise
    # reintroduce it every week, and Ganevat's negoce-from-outside-the-Jura is
    # exactly the row whose price says nothing about the bottles being hunted.
    # It is still in hits.json, which is the run's full record.
    reportable = [h for h in all_hits if h.get("alertable") is not False]

    if alerting:
        shown, kind = alerting, {}
        subject = subject_for(DIGEST_SUBJECT, alerting)
    elif force:
        shown, kind = reportable, {"on_demand": True}
        subject = subject_for(ONDEMAND_SUBJECT, reportable)
    elif reportable and recap_due(state, now):
        shown, kind = reportable, {"recap": True}
        subject = subject_for(RECAP_SUBJECT, reportable)
    else:
        # Nothing was alerted, so nothing is being silenced; persisting here
        # just refreshes last_price for future drop comparisons.
        if not dry_run:
            save_state(updated_state, state_path)
        print("No newly alert-worthy hits this run (cooldown or no change) -- silent run is valid.")
        return alerting

    body = build_digest_body(shown, notes, tables=tables, **kind)
    html = build_digest_html(shown, notes, tables=tables, **kind)

    if dry_run:
        print("DRY_RUN=1 set, skipping SMTP send and leaving state untouched.")
        print(f"{subject} would be:\n")
        print(body)
        return alerting

    send_email(body, subject=subject, html=html)
    # The recap clock is reset by whichever email went out, so a digest and a
    # recap can never both fire for the same stretch of quiet.
    save_state(_stamp_recap(updated_state, now), state_path)
    if alerting:
        print(f"Sent digest email with {len(alerting)} alert-worthy hit(s).")
    elif force:
        print(f"Run requested by hand; reported {len(all_hits)} currently "
              f"matched hit(s), nothing marked as alerted.")
    else:
        print(f"Nothing new for {RECAP_DAYS} days; sent a recap of "
              f"{len(all_hits)} currently matched hit(s).")
    return alerting
