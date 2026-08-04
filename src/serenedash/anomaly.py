"""serenedash.anomaly — what has changed, judged against the series' own recent past.

Every other finding in this tool is a comparison against a threshold somebody chose: WAL over 1x the
database, memory_limit over 75% of RAM. Those catch conditions that are wrong at any moment. They
cannot catch the shapes that are only wrong relative to what this server normally does — a pool that
has been climbing all afternoon is not over any limit until it is, and a step change in RSS at 3am
is invisible to a rule that only knows one number.

## Why rules and not a model

The obvious answer is "train something". There is nothing to train on: nobody has labelled these
series, an unsupervised detector fitted on one deployment's normal transfers nothing to the next,
and a highlight on this dashboard that cannot say WHY is worse than no highlight at all — the whole
discipline here is that a panel says only what was measured. So this is an explicit decision
procedure over robust statistics. Each detection carries the baseline it was judged against, the
value that arrived, and the window, which is what makes it arguable rather than authoritative.

## Why median and MAD rather than mean and standard deviation

Both estimators would be computed over a window that CONTAINS the event being looked for. A mean is
dragged toward the spike and a standard deviation is inflated by it, so a single large excursion
raises the bar enough to hide itself — the failure gets worse exactly as the event gets bigger. The
median and the median absolute deviation have a 50% breakdown point: up to half the window can be
arbitrary without moving them. On a 160-sample history that is the difference between catching the
first big allocation and catching none of them.
"""
import statistics

# 1.4826 makes MAD an estimator of sigma for normally distributed data, so the k below can be read
# in the usual units. The data are not normal — which is the point of using MAD at all — but the
# scaling keeps `k = 6` meaning roughly what a reader expects it to mean.
MAD_TO_SIGMA = 1.4826

# How far from the baseline counts. 6 is deliberately blunt: this highlights a row on a dashboard
# somebody is watching, and a detector that fires on ordinary variation trains its reader to ignore
# it, which costs more than the detections are worth.
K = 6.0

# Absolute floors, per unit. Without them a series that is genuinely flat has a MAD of zero and
# every rounding wobble is infinitely many sigmas — 8 MiB of allocator noise reported as an anomaly
# against a 34 GB pool.
FLOOR = {"bytes": 64 * 2**20, "percent": 5.0}

# Minimum samples before each rule may speak. A spike needs a baseline to be a spike against; a
# trend needs enough of a window that a query starting is not a trend.
MIN_SPIKE, MIN_TREND = 24, 40

# Growth over the window, and how much of it has to be in one direction, for the leak shape. A
# monotonic climb is what separates "a big query ran" from "this has not come back down since
# lunchtime"; without the direction test, one large allocation and its release looks the same.
GROWTH_FRAC, GROWTH_STEPS = 0.30, 0.80

# And how much of the window has to have actually risen, as opposed to merely not fallen. See the
# note in scan_series: without this, one jump at the end of a flat series is a perfect trend.
GROWTH_RISING = 0.25


SERIES = {
    "mem": ("duckdb_memory()", "bytes"),
    "rss": ("resident memory", "bytes"),
    "swap": ("paged out", "bytes"),
    "cpu": ("process CPU", "percent"),
}


# The rendered label a series appears under. Anything not here is a memory pool, whose row label is
# the tag itself — so `series_of` is what lets the renderer and the tooltip get from a row back to
# the series that was measured, without either of them carrying its own copy of the mapping.
LABELS = {"in use": "mem", "resident": "rss", "swapped": "swap", "cpu": "cpu"}


def series_of(label):
    label = (label or "").strip()
    return LABELS.get(label) or (f"t:{label}" if label else "")


def describe(key):
    """(human name, unit) for a series key. `t:TAG` is a per-pool trace."""
    if key.startswith("t:"):
        return f"memory pool {key[2:]}", "bytes"
    return SERIES.get(key, (key, "bytes"))


def fmt(v, unit):
    if unit == "percent":
        return f"{v:.0f}%"
    n = float(v)
    for u in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


class Anomaly:
    """One detection, carrying everything needed to disagree with it."""

    def __init__(self, key, rule, value, baseline, window, detail):
        self.key, self.rule = key, rule
        self.value, self.baseline, self.window = value, baseline, window
        self.detail = detail
        self.name, self.unit = describe(key)

    def label(self):
        """Two words for the row it is on. The panel has no space for the reasoning."""
        return {"spike": "spike", "shift": "step up" if self.value > self.baseline else "step down",
                "growth": "climbing"}[self.rule]

    def line(self):
        """One line for a human: what arrived, what was expected, over how long."""
        return (f"{self.name} {fmt(self.value, self.unit)} against a baseline of "
                f"{fmt(self.baseline, self.unit)} over the last {self.window} samples")

    def as_finding(self):
        return {
            "what": f"anomaly: {self.name} {self.label()}",
            "detail": self.detail,
            "series": self.key, "rule": self.rule,
            "value": round(self.value, 2), "baseline": round(self.baseline, 2),
            "window_samples": self.window,
            "note": "judged against this series' own recent past, not a fixed threshold. The "
                    "baseline is a median and the spread a median absolute deviation, so the event "
                    "being looked for cannot inflate the bar it has to clear.",
        }

    def __repr__(self):                                          # pragma: no cover - debugging only
        return f"<Anomaly {self.key} {self.rule} {self.value:.0f} vs {self.baseline:.0f}>"


def spread(vals, unit):
    """(median, scaled MAD) with the unit's floor applied. The floor is the whole trick on a flat
    series: MAD is then zero, and every last-digit wobble would be an infinite number of sigmas."""
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) * MAD_TO_SIGMA
    return med, max(mad, FLOOR.get(unit, 0.0))


def clean(vals):
    return [float(v) for v in vals if v is not None]


def scan_series(key, vals):
    """Every rule, in order of how specific the shape is. At most one detection per series.

    Ordered because they overlap: a step up is also, in its first sample, a spike. Reporting both
    for one event is two rows saying one thing, and the more specific description is the useful one.
    """
    vals = clean(vals)
    name, unit = describe(key)
    n = len(vals)
    if n < MIN_SPIKE or not any(vals):
        return None
    now = vals[-1]

    # Sustained shift: the recent quarter against everything before it. Checked before the spike
    # rule because a level change is a different event from an excursion, and it is the one that
    # survives — a spike that does not come back down was never a spike.
    if n >= MIN_TREND:
        q = max(4, n // 4)
        recent, before = vals[-q:], vals[:-q]
        med, sd = spread(before, unit)
        rmed = statistics.median(recent)
        if abs(rmed - med) > K * sd:
            return Anomaly(key, "shift", rmed, med, n,
                           f"{name} has held around {fmt(rmed, unit)} for the last {q} samples, "
                           f"against {fmt(med, unit)} over the {len(before)} before them — a level "
                           f"change, not an excursion. Deviation is "
                           f"{abs(rmed - med) / sd:.1f}x the series' own spread.")

    # Leak shape: mostly one direction, and materially higher than it started. Two conditions
    # because either alone is ordinary — everything drifts, and everything occasionally doubles.
    if n >= MIN_TREND:
        first = statistics.median(vals[:max(4, n // 8)])
        steps = [b - a for a, b in zip(vals[-MIN_TREND:], vals[-MIN_TREND + 1:], strict=False)]
        up = sum(1 for d in steps if d >= 0) / max(1, len(steps))
        # Steps that actually moved, not steps that merely did not fall. A flat series with one
        # jump at the end is non-decreasing in 100% of its steps, so the direction test alone
        # called a single 22 GB allocation a leak. A leak arrives in many increments; that is the
        # whole difference between it and a step.
        rose = sum(1 for d in steps if d > 0) / max(1, len(steps))
        grew = now - first
        if (first > 0 and grew > first * GROWTH_FRAC and grew > FLOOR.get(unit, 0)
                and up >= GROWTH_STEPS and rose >= GROWTH_RISING):
            return Anomaly(key, "growth", now, first, n,
                           f"{name} has risen from {fmt(first, unit)} to {fmt(now, unit)} "
                           f"({grew / first * 100:.0f}%), rising in {rose * 100:.0f}% of the last "
                           f"{len(steps)} samples and falling in {(1 - up) * 100:.0f}%. That is "
                           f"the shape of something not being released, as opposed to a query "
                           f"holding memory and giving it back.")

    # Excursion: the newest sample alone, against the window behind it.
    med, sd = spread(vals[:-1], unit)
    if abs(now - med) > K * sd:
        return Anomaly(key, "spike", now, med, n,
                       f"{name} is {fmt(now, unit)} against a baseline of {fmt(med, unit)} over "
                       f"the previous {n - 1} samples — {abs(now - med) / sd:.1f}x the spread. One "
                       f"sample, so it may be an excursion rather than a change.")
    return None


def scan(hist):
    """Every series in the history. Sorted so the output is stable between ticks."""
    out = []
    for key in sorted(hist or {}):
        found = scan_series(key, hist.get(key) or [])
        if found:
            out.append(found)
    return out


def index(hist):
    """{series key: Anomaly} — for the renderer, which needs to look up one row at a time."""
    return {a.key: a for a in scan(hist)}
