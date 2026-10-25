"""
GFS retention by count plus a minimum age.

GFS decides which sets prune may delete; the target's default retention decides the earliest date it can. Only
verified sets (level 1 passed) count toward the buckets, and the newest verified set is never pruned. Once any QA
restore has passed, keep_min_verified keeps that many sets with a passed QA restore.
"""
from dataclasses import dataclass
from zoneinfo import ZoneInfo


@dataclass
class Candidate:
    set_id: str
    created: object  # aware datetime
    verified: bool
    qa_passed: bool = False


def _buckets(sets, count, key):
    """
    Keep the newest set of each of the `count` most recent periods that contain a set.
    """
    kept = {}
    seen = []
    for s in sets:
        period = key(s.created)
        if period in seen:
            continue
        if len(seen) >= count:
            break
        seen.append(period)
        kept[s.set_id] = period
    return kept


def plan(candidates, retention, now, time_zone='UTC'):
    """
    Returns (keep, prune): keep maps set_id to the reasons it is kept, prune lists set_ids, newest first.
    """
    tz = ZoneInfo(time_zone or 'UTC')
    ordered = sorted(candidates, key=lambda c: c.created, reverse=True)
    verified = [c for c in ordered if c.verified]
    local = lambda c: c.astimezone(tz)  # noqa: E731
    keep = {}

    def add(set_id, reason):
        keep.setdefault(set_id, []).append(reason)

    for c in ordered:
        if now - c.created < retention.min_age:
            add(c.set_id, 'min_age')
    if verified:
        add(verified[0].set_id, 'newest verified')
    for c in verified[:retention.keep_last]:
        add(c.set_id, 'last')
    rules = (
        ('daily', retention.keep_daily, lambda d: local(d).date()),
        ('weekly', retention.keep_weekly, lambda d: local(d).isocalendar()[:2]),
        ('monthly', retention.keep_monthly, lambda d: (local(d).year, local(d).month)),
        ('yearly', retention.keep_yearly, lambda d: local(d).year),
    )
    for name, count, key in rules:
        for set_id in _buckets(verified, count, key):
            add(set_id, name)
    qa = [c for c in ordered if c.qa_passed]
    for c in qa[:retention.keep_min_verified]:
        add(c.set_id, 'qa restore')
    prune = [c.set_id for c in ordered if c.set_id not in keep]
    return keep, prune
