#!/usr/bin/env python3
"""
Daily Estonian electricity price checker.

Fetches tomorrow's Nord Pool day-ahead prices for Estonia (EE) from the
public Elering API, finds the cheapest non-overlapping time windows for
your appliance runs, sends the result via ntfy.sh push notification, and
writes an HTML report (for GitHub Pages).

Edit the JOBS list and PRICE_THRESHOLD below to match your own appliances.
Run locally to test:  NTFY_TOPIC=your-topic python3 check_prices.py
"""

import os
import time
import math
import json
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Price-band colours — a phosphor-meter palette (green/amber/red LED semantics)
BAND_COLORS = {"cheap": "#5FE8A3", "mid": "#F0B429", "high": "#F0665A"}
JOB_COLORS = ["#6FA8FF", "#C792EA", "#FF9F68", "#7DE0E6", "#F2789F"]

# ---------------------------------------------------------------------------
# CONFIG — edit these to match your appliances
# ---------------------------------------------------------------------------

# Each entry = one appliance run you want scheduled, in MINUTES.
# Example: two 4h laundry (washer+dryer) loads + one 2.5h dishwasher run.
JOBS = [
    {"name": "Laundry load 1", "minutes": 240},
    {"name": "Laundry load 2", "minutes": 240},
    {"name": "Dishwasher", "minutes": 150},
]

PRICE_THRESHOLD = 100.0  # EUR/MWh — your "cheap" cutoff

TIMEZONE = "Europe/Tallinn"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")  # set as a GitHub Actions secret
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}" if NTFY_TOPIC else None

OUTPUT_HTML = os.environ.get("OUTPUT_HTML", "docs/index.html")

ELERING_URL = "https://dashboard.elering.ee/api/nps/price"

MAX_RETRIES = 6
RETRY_SLEEP_SECONDS = 300  # 5 min between retries if tomorrow's data isn't published yet

# ---------------------------------------------------------------------------


def fetch_tomorrow_prices():
    tz = ZoneInfo(TIMEZONE)
    now_local = datetime.now(tz)
    tomorrow_local = (now_local + timedelta(days=1)).date()

    start_local = datetime(tomorrow_local.year, tomorrow_local.month, tomorrow_local.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1) - timedelta(seconds=1)

    start_utc = start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_utc = end_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.999Z")

    url = f"{ELERING_URL}?start={start_utc}&end={end_utc}&fields=ee"

    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "electricity-watch-script/1.0"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())

    raw_slots = payload.get("data", {}).get("ee", [])
    slots = [
        {
            "dt": datetime.fromtimestamp(s["timestamp"], tz=ZoneInfo("UTC")).astimezone(tz),
            "price": float(s["price"]),
        }
        for s in raw_slots
    ]
    # keep only slots that actually fall on tomorrow (API sometimes pads a boundary slot)
    slots = [s for s in slots if s["dt"].date() == tomorrow_local]
    slots.sort(key=lambda s: s["dt"])
    return tomorrow_local, slots


def slot_minutes(slots):
    if len(slots) < 2:
        return 60
    delta = slots[1]["dt"] - slots[0]["dt"]
    return max(1, int(delta.total_seconds() // 60))


def best_window(slots, used, need_slots):
    """Cheapest contiguous run of `need_slots` slots, skipping already-used indices."""
    best = None
    n = len(slots)
    for i in range(0, n - need_slots + 1):
        idx_range = range(i, i + need_slots)
        if any(j in used for j in idx_range):
            continue
        avg = sum(slots[j]["price"] for j in idx_range) / need_slots
        if best is None or avg < best["avg"]:
            best = {"start_idx": i, "end_idx": i + need_slots - 1, "avg": avg}
    return best


def schedule_jobs(slots, jobs):
    step = slot_minutes(slots)
    used = set()
    results = []
    for job in jobs:
        need = max(1, math.ceil(job["minutes"] / step))
        win = best_window(slots, used, need)
        if win is None:
            results.append({**job, "error": "no free window long enough left"})
            continue
        for j in range(win["start_idx"], win["end_idx"] + 1):
            used.add(j)
        start_dt = slots[win["start_idx"]]["dt"]
        end_dt = slots[win["end_idx"]]["dt"] + timedelta(minutes=step)
        results.append({**job, "start": start_dt, "end": end_dt, "avg_price": win["avg"]})
    return results


def cheap_ranges(slots, threshold):
    ranges, cur = [], None
    for s in slots:
        if s["price"] < threshold:
            cur = [s["dt"], s["dt"], [s["price"]]] if cur is None else [cur[0], s["dt"], cur[2] + [s["price"]]]
        else:
            if cur is not None:
                ranges.append(cur)
                cur = None
    if cur is not None:
        ranges.append(cur)
    step = slot_minutes(slots)
    return [
        {"start": r[0], "end": r[1] + timedelta(minutes=step), "avg_price": sum(r[2]) / len(r[2])}
        for r in ranges
    ]


def build_report(day, slots):
    if not slots:
        text = (
            f"Tomorrow's ({day}) prices aren't published on Elering yet.\n"
            "This usually means the check ran before the ~13:00-15:00 CET/EET publish window."
        )
        return text, None

    prices = [s["price"] for s in slots]
    avg_price, min_price, max_price = sum(prices) / len(prices), min(prices), max(prices)
    cheap = cheap_ranges(slots, PRICE_THRESHOLD)
    jobs_result = schedule_jobs(slots, JOBS)

    lines = [f"Estonia electricity - {day}", "", f"Avg {avg_price:.1f} / min {min_price:.1f} / max {max_price:.1f} EUR/MWh", ""]

    if cheap:
        lines.append(f"Cheap periods (< {PRICE_THRESHOLD:.0f} EUR/MWh):")
        for c in cheap:
            lines.append(f"  {c['start'].strftime('%H:%M')}-{fmt_hm(c['end'], day)} (avg {c['avg_price']:.1f})")
    else:
        lines.append(f"No hours below {PRICE_THRESHOLD:.0f} EUR/MWh tomorrow.")
    lines.append("")

    lines.append("Suggested non-overlapping slots:")
    for r in jobs_result:
        if "error" in r:
            lines.append(f"  {r['name']}: {r['error']}")
        else:
            flag = "OK" if r["avg_price"] < PRICE_THRESHOLD else "!!"
            lines.append(
                f"  [{flag}] {r['name']} ({r['minutes']}min): "
                f"{r['start'].strftime('%H:%M')}-{fmt_hm(r['end'], day)} (avg {r['avg_price']:.1f} EUR/MWh)"
            )

    return "\n".join(lines), {
        "day": day, "avg": avg_price, "min": min_price, "max": max_price,
        "cheap": cheap, "jobs": jobs_result, "slots": slots,
    }


def send_ntfy(text):
    if not NTFY_URL:
        print("NTFY_TOPIC not set - skipping push notification.")
        return
    req = urllib.request.Request(
        NTFY_URL,
        data=text.encode("utf-8"),
        method="POST",
        headers={"Title": "Electricity prices tomorrow", "Priority": "default", "Tags": "zap"},
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        print("ntfy notification sent.")
    except urllib.error.URLError as e:
        print(f"Failed to send ntfy notification: {e}")


PAGE_HEAD = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Electricity watch - Estonia</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;700&family=JetBrains+Mono:wght@400;500;700&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --graphite-900: #10151B;
    --graphite-800: #1B232C;
    --hairline: #2A343E;
    --ink: #E8EDF2;
    --ink-muted: #7C8894;
    --cheap: #5FE8A3;
    --mid: #F0B429;
    --high: #F0665A;
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    background: var(--graphite-900);
    color: var(--ink);
    font-family: 'Inter', system-ui, sans-serif;
    max-width: 720px;
    margin: 0 auto;
    padding: 1.75rem 1.25rem 3rem;
    line-height: 1.45;
  }
  a { color: var(--cheap); }
  .eyebrow {
    font-family: 'Space Grotesk', system-ui, sans-serif;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    font-size: 0.78rem;
    color: var(--ink-muted);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .headline {
    display: flex;
    align-items: baseline;
    gap: 0.9rem;
    margin: 1.1rem 0 0.3rem;
    flex-wrap: wrap;
  }
  .headline .num {
    font-family: 'JetBrains Mono', monospace;
    font-weight: 700;
    font-size: 3.1rem;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.02em;
  }
  .headline .unit {
    font-size: 0.95rem;
    color: var(--ink-muted);
  }
  .pill {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    font-weight: 500;
    padding: 0.25rem 0.6rem;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  .pill.cheap { background: rgba(95,232,163,0.15); color: var(--cheap); }
  .pill.mid   { background: rgba(240,180,41,0.15); color: var(--mid); }
  .pill.high  { background: rgba(240,102,90,0.18); color: var(--high); }
  .stats {
    display: flex;
    gap: 1.5rem;
    margin: 0.4rem 0 1.6rem;
    color: var(--ink-muted);
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
  }
  .stats b { color: var(--ink); font-weight: 500; }
  .panel {
    background: var(--graphite-800);
    border: 1px solid var(--hairline);
    border-radius: 14px;
    padding: 1.1rem 1.1rem 0.9rem;
  }
  .panel svg { width: 100%; height: auto; display: block; }
  .ticks {
    display: flex;
    justify-content: space-between;
    margin-top: 0.5rem;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.72rem;
    color: var(--ink-muted);
  }
  .lanes { margin-top: 1.1rem; }
  .lane { margin-top: 0.65rem; }
  .lane:first-child { margin-top: 0; }
  .lane-label {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.75rem;
    font-weight: 500;
    margin-bottom: 0.3rem;
  }
  .lane-track {
    position: relative;
    height: 10px;
    background: var(--hairline);
    border-radius: 999px;
    overflow: hidden;
  }
  .lane-fill {
    position: absolute;
    top: 0;
    bottom: 0;
    border-radius: 999px;
  }
  .section-label {
    font-family: 'Space Grotesk', system-ui, sans-serif;
    font-weight: 500;
    font-size: 0.95rem;
    margin: 1.7rem 0 0.7rem;
    color: var(--ink);
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0.7rem;
  }
  .card {
    background: var(--graphite-800);
    border: 1px solid var(--hairline);
    border-left: 3px solid var(--job-color, var(--cheap));
    border-radius: 10px;
    padding: 0.7rem 0.85rem;
  }
  .card .name { font-size: 0.85rem; color: var(--ink-muted); margin-bottom: 0.3rem; }
  .card .time {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.05rem;
    font-variant-numeric: tabular-nums;
  }
  .card .price {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.8rem;
    color: var(--ink-muted);
    margin-top: 0.2rem;
  }
  .cheap-list {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem;
    color: var(--ink-muted);
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 0.9rem;
  }
  .cheap-list span { color: var(--cheap); }
  .empty {
    background: var(--graphite-800);
    border: 1px dashed var(--hairline);
    border-radius: 14px;
    padding: 2rem 1.2rem;
    text-align: center;
    color: var(--ink-muted);
  }
  .empty .bolt { font-size: 1.6rem; margin-bottom: 0.4rem; }
  footer {
    margin-top: 2.2rem;
    color: var(--ink-muted);
    font-size: 0.78rem;
    font-family: 'JetBrains Mono', monospace;
  }
  @media (prefers-reduced-motion: no-preference) {
    .panel, .card { transition: border-color 0.2s ease; }
  }
</style></head>
<body>
"""

PAGE_FOOT = "</body></html>"


def price_band(price, threshold):
    if price < threshold:
        return "cheap"
    if price < threshold * 2:
        return "mid"
    return "high"


def fmt_hm(dt, day):
    """Format a time as HH:MM, showing 24:00 instead of 00:00 when it's really next-day midnight."""
    return "24:00" if dt.date() != day else dt.strftime("%H:%M")


def build_chart_parts(data, threshold):
    """Returns (svg, ticks_html, lanes_html). Text lives in normal HTML (rem-sized,
    stays legible on phones) while the SVG only draws bars/lines (scales cleanly)."""
    slots = data["slots"]
    n = len(slots)
    max_price = max(s["price"] for s in slots) or 1.0
    bars_h = 190

    bar_w = 1000 / n
    gap = min(2.0, bar_w * 0.18)

    svg = [f'<svg viewBox="0 0 1000 {bars_h}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Hourly price chart" preserveAspectRatio="none">']

    if threshold < max_price:
        y_thresh = bars_h - (threshold / max_price) * bars_h
        svg.append(
            f'<line x1="0" y1="{y_thresh:.1f}" x2="1000" y2="{y_thresh:.1f}" '
            f'stroke="var(--ink-muted)" stroke-width="1" stroke-dasharray="5 5" opacity="0.5"/>'
        )

    for i, s in enumerate(slots):
        h = max(3.0, (s["price"] / max_price) * bars_h)
        x = i * bar_w + gap / 2
        y = bars_h - h
        color = BAND_COLORS[price_band(s["price"], threshold)]
        svg.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(0.5, bar_w - gap):.2f}" height="{h:.2f}" rx="1.5" fill="{color}"/>')

    svg.append(f'<line x1="0" y1="{bars_h}" x2="1000" y2="{bars_h}" stroke="var(--hairline)" stroke-width="1.5"/>')
    svg.append("</svg>")

    # hour ticks — plain HTML flex row so font-size is a real rem value on every screen
    step = max(1, round(n / 6))
    tick_spans = "".join(f'<span>{slots[i]["dt"].strftime("%H:%M")}</span>' for i in range(0, n, step))
    ticks_html = f'<div class="ticks">{tick_spans}</div>'

    # appliance lanes — HTML rows with a percentage-positioned fill bar under the label
    day_start = slots[0]["dt"].replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    for idx, job in enumerate(data["jobs"]):
        color = JOB_COLORS[idx % len(JOB_COLORS)]
        if "error" in job:
            rows.append(
                f'<div class="lane"><div class="lane-label" style="color:var(--ink-muted)">'
                f'{job["name"]}: no free slot long enough</div></div>'
            )
            continue
        frac_start = (job["start"] - day_start).total_seconds() / 86400
        frac_end = (job["end"] - day_start).total_seconds() / 86400
        left_pct = max(0.0, frac_start * 100)
        width_pct = max(0.6, min(100.0, frac_end * 100) - left_pct)
        label = (
            f'{job["name"]} \u00b7 {job["start"].strftime("%H:%M")}\u2013{fmt_hm(job["end"], data["day"])} '
            f'\u00b7 {job["avg_price"]:.0f} EUR/MWh'
        )
        rows.append(
            f'<div class="lane">'
            f'<div class="lane-label" style="color:{color}">{label}</div>'
            f'<div class="lane-track"><div class="lane-fill" style="left:{left_pct:.2f}%;width:{width_pct:.2f}%;background:{color}"></div></div>'
            f'</div>'
        )
    lanes_html = f'<div class="lanes">{"".join(rows)}</div>'

    return "\n".join(svg), ticks_html, lanes_html


def write_html(text_report, data):
    os.makedirs(os.path.dirname(OUTPUT_HTML) or ".", exist_ok=True)
    updated = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M %Z")

    if data is None:
        body = f"""
<div class="eyebrow"><span>&#9889; Electricity watch</span><span>Estonia</span></div>
<div class="empty">
  <div class="bolt">&#9889;</div>
  <div>Tomorrow's prices aren't published yet.</div>
  <div>Elering usually publishes them between 13:00 and 15:00 CET/EET.<br>The next scheduled check will pick them up automatically.</div>
</div>
<footer>Checked {updated}</footer>
"""
    else:
        band = price_band(data["avg"], PRICE_THRESHOLD)
        chart_svg, ticks_html, lanes_html = build_chart_parts(data, PRICE_THRESHOLD)

        cards = "".join(
            f"""<div class="card" style="--job-color:{JOB_COLORS[i % len(JOB_COLORS)]}">
                  <div class="name">{r['name']}</div>
                  {'<div class="time" style="color:var(--ink-muted);font-size:0.85rem">no free slot long enough</div>' if 'error' in r else
                   f'<div class="time">{r["start"].strftime("%H:%M")}&ndash;{fmt_hm(r["end"], data["day"])}</div>'
                   f'<div class="price">avg {r["avg_price"]:.1f} EUR/MWh</div>'}
                </div>"""
            for i, r in enumerate(data["jobs"])
        )

        cheap_html = "".join(
            f'<span>{c["start"].strftime("%H:%M")}&ndash;{fmt_hm(c["end"], data["day"])}</span>'
            for c in data["cheap"]
        ) or '<span style="color:var(--ink-muted)">none today</span>'

        body = f"""
<div class="eyebrow"><span>&#9889; Electricity watch</span><span>{data['day']}</span></div>
<div class="headline">
  <span class="num">{data['avg']:.0f}</span>
  <span class="unit">EUR/MWh avg</span>
  <span class="pill {band}">{band}</span>
</div>
<div class="stats">min <b>{data['min']:.0f}</b> &middot; max <b>{data['max']:.0f}</b> &middot; cutoff <b>{PRICE_THRESHOLD:.0f}</b></div>

<div class="panel">{chart_svg}{ticks_html}{lanes_html}</div>

<div class="section-label">Suggested slots</div>
<div class="cards">{cards}</div>

<div class="section-label">Cheap windows (&lt; {PRICE_THRESHOLD:.0f} EUR/MWh)</div>
<div class="cheap-list">{cheap_html}</div>

<footer>Updated {updated} &middot; data via Elering / Nord Pool</footer>
"""

    html = PAGE_HEAD + body + PAGE_FOOT

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {OUTPUT_HTML}")

    # Tell GitHub Pages to skip the Jekyll build and serve this folder as plain
    # static files — without this, Pages tries to run Jekyll over docs/ and can
    # fail with "No such file or directory" style errors.
    nojekyll_path = os.path.join(os.path.dirname(OUTPUT_HTML) or ".", ".nojekyll")
    open(nojekyll_path, "a").close()


def main():
    day, slots = None, []
    for attempt in range(1, MAX_RETRIES + 1):
        day, slots = fetch_tomorrow_prices()
        if slots:
            break
        print(f"Attempt {attempt}/{MAX_RETRIES}: tomorrow's prices not published yet, waiting...")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_SLEEP_SECONDS)

    text_report, data = build_report(day, slots)
    print(text_report)
    send_ntfy(text_report)
    write_html(text_report, data)


if __name__ == "__main__":
    main()
