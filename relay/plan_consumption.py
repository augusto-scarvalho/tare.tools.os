"""What 1% of a subscription window bought, measured on the owner's own account (shown by `tare quota`).

tare reads each plan's usage meter (provider_quota) and knows the API-equivalent dollars of its own calls
(provider_spend.equivalents). Use outside tare only ever adds to the meter, so between two readings of one window
`used_delta >= rate * U - resolution` and every such span bounds the rate from above: `rate <= (used_delta +
resolution) / U`. The estimate is the smallest bound of the last 7 days; the cleanest span (least outside use) wins and
outside use on other spans cannot move it. The vendor changes quotas without notice, so older spans are never used.
"""
import json
import time
from bisect import bisect_right
from datetime import datetime

WEEK = 7 * 86400
# The meter's step per source (Claude and Codex report whole percents, AGY four decimals): the default resolution
# until the series shows a smaller non-zero step of its own.
STEP = {'claude': 0.01, 'codex': 0.01, 'agy': 0.0001}
# API-equivalent dollars a span needs to count: below it one meter step outweighs the use, and the bound says nothing.
U_FLOOR = 5.0
# Dollars the best span must cover for 'firm': enough meter steps that quantisation no longer decides it.
U_FIRM = 50.0
# How far the halves of the best span may fall short of its bound for 'firm': one meter step on a span a few steps long.
TOLERANCE = 0.25
# A window's reset time can drift by seconds between readings of the same window; a new window moves it by hours.
SAME_WINDOW = 3600


def history_path():
    from .native_installation import storage_root
    return storage_root()/'serve'/'plan-readings.json'


def history():
    try:
        rows = json.loads(history_path().read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return rows if isinstance(rows, list) else []


def keep(accounts, now, done=None):
    """Append the buckets of every subscription reading that succeeded (a failed one has none) and drop what is older
    than the rolling window. A reading takes seconds: `now` is when it began and `done` when it ended, and the meter
    was sampled somewhere between. ponytail: two tare processes refreshing at once can lose one append; a lost reading
    only makes a span longer."""
    rows = [{'at': now, 'done': now if done is None else done, 'account': a['account'], 'bucket': b['id'],
             'remaining': b['remaining'], 'resets_at': b.get('resets_at')}
            for a in accounts if a['kind'] == 'subscription' and a['status'] == 'OBSERVED' for b in a.get('buckets') or []]
    if rows:
        from .relay_io import write_json_atomic
        history_path().parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(history_path(), [r for r in history() if isinstance(r, dict) and now - WEEK <= r.get('at', 0)] + rows)


def estimate(points, uses, step, now):
    """points: [(time the reading began, used fraction, resets_at, time it ended (optional: an instant reading))] of
    one window; uses: [(since, recorded at, API-equivalent dollars)] of tare's calls that count toward it ((time,
    dollars) when the call was instant). {'state': 'none' | 'weak' | 'firm', and, unless none: 'usd_per_percent' (a
    lower bound on the dollars 1% bought), 'measured_from', 'measured_to', 'covered_usd'}."""
    points = sorted(p for p in points if now - WEEK <= p[0] <= now and p[2] is not None)
    uses = sorted(((u[0], *u) if len(u) == 2 else u for u in uses if now - WEEK <= u[-2] <= now), key=lambda u: u[1])
    sums = {}

    def spent(i, t):
        """Dollars of the calls that lie wholly inside the span from point i to time t. A session's calls are recorded
        when it ends: one that began before the span did may have moved the meter before it too, and counting it would
        make the plan look better than it is. Likewise a call made while a reading was being taken may or may not be
        in that reading: only calls that began after the first reading ended and were recorded before the second
        began count. Leaving a call out only loosens the bound, which is the safe side."""
        if i not in sums:
            ended = points[i][3] if len(points[i]) > 3 else points[i][0]
            inside = [u for u in uses if u[0] >= ended]
            total = [0.0]
            for u in inside:
                total.append(total[-1] + u[2])
            sums[i] = [u[1] for u in inside], total
        times, total = sums[i]
        return total[bisect_right(times, t)]
    same = lambda a, b: abs(a[2] - b[2]) < SAME_WINDOW
    steps = [round(b[1] - a[1], 6) for a, b in zip(points, points[1:]) if same(a, b) and b[1] > a[1]]
    resolution = min(steps, default=step)

    def span(i, j):  # (bound, dollars) of points i..j, or None when it is no usable span
        a, b = points[i], points[j]
        # A meter at its limit stops counting, so a span ending there would understate the use: not a bound.
        if same(a, b) and a[1] <= b[1] < 1 and (u := spent(i, b[0])) >= U_FLOOR:
            return (b[1] - a[1] + resolution) / u, u
    spans = sorted((*found, i, j) for i in range(len(points)) for j in range(i + 1, len(points)) if (found := span(i, j)))
    if not spans:
        return {'state': 'none'}
    bound, covered, i, j = spans[0]
    # Firm: the best span reaches U_FIRM and its two halves, each a span of its own, reproduce its bound within
    # TOLERANCE. ponytail: halves of the best span stand for "two tightest bounds agree": nested spans always agree.
    halves = [max(x[0], y[0]) for k in range(i + 1, j) if (x := span(i, k)) and (y := span(k, j))]
    firm = covered >= U_FIRM and halves and min(halves) <= bound * (1 + TOLERANCE)
    return {'state': 'firm' if firm else 'weak', 'usd_per_percent': round(0.01 / bound, 2),
            'measured_from': points[i][0], 'measured_to': points[j][0], 'covered_usd': round(covered, 2)}


def annotate(accounts, now):
    """Put the estimate on every bucket of every observed subscription: bucket['consumption']."""
    from . import provider_spend
    from .provider_quota import covers
    saved = [r for r in history() if isinstance(r, dict)]
    for reading in accounts:
        if reading['kind'] != 'subscription' or reading['status'] != 'OBSERVED':
            continue
        name = reading['account']
        calls = provider_spend.equivalents(name, now - WEEK)
        for bucket in reading.get('buckets') or []:
            points = [(r['at'], 1 - r['remaining'], r['resets_at'],
                       r['done'] if type(r.get('done')) in (int, float) else r['at']) for r in saved
                      if r.get('account') == name and r.get('bucket') == bucket['id']
                      and type(r.get('at')) in (int, float) and type(r.get('remaining')) in (int, float)
                      and type(r.get('resets_at')) in (int, float)]
            uses = [(since, at, usd) for since, at, usd, model in calls if since is not None and covers(name, bucket, model)]
            bucket['consumption'] = estimate(points, uses, STEP.get(name, 0.01), now)


def line(consumption):
    """The line `tare quota` prints under a bucket."""
    if not consumption or consumption['state'] == 'none':
        return 'no measure yet'
    a, b = (datetime.fromtimestamp(consumption[k]) for k in ('measured_from', 'measured_to'))
    when = f'{a.day} to {b.day} {b:%b}' if (a.year, a.month) == (b.year, b.month) else f'{a.day} {a:%b} to {b.day} {b:%b}'
    return (f"{'about' if consumption['state'] == 'firm' else 'at least'} US$ {consumption['usd_per_percent']:g} of "
            f"API-equivalent use per 1% ({consumption['state']}, measured {when})")
