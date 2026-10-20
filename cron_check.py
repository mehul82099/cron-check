#!/usr/bin/env python3
"""cron-check: parse, explain, and audit cron expressions the way cron actually runs them.

A faithful Python implementation of Vixie-cron (cronie) field semantics:

- Field parsing mirrors cronie's src/entry.c (get_list/get_range/get_number):
  '*' = first-last, inclusive ranges, lists, steps within the field only,
  month/weekday names (case-insensitive), day-of-week 0 and 7 both Sunday,
  the "low > high && high == 0 -> high = 7" Sunday-wrap hack, reversed ranges
  silently matching nothing (no error!), and '*/n' setting the field's STAR
  flag because the STAR flag depends only on the field's first character.
- Matching mirrors cronie's src/cron.c: minute/hour/month must match, and the
  two day fields combine by OR only when NEITHER has the STAR flag:
      (DOM_STAR or DOW_STAR) ? (dom && dow) : (dom || dow)
- That means '0 0 */2 * 1' fires on Mondays that ALSO fall on odd days of the
  month, while '0 0 1-31/2 * 1' fires on Mondays OR odd days. This tool
  surfaces exactly that.

Nothing here shells out and nothing touches the network.
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

__version__ = "1.0.0"

MINUTE = ("minute", 0, 59)
HOUR = ("hour", 0, 23)
DOM = ("day of month", 1, 31)
MONTH = ("month", 1, 12)
DOW = ("day of week", 0, 7)

MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun",
               "jul", "aug", "sep", "oct", "nov", "dec"]
DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]

AT_FORMS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

SEARCH_DAYS = 8 * 366  # two full leap cycles: covers Feb 29 schedules


class CronError(ValueError):
    pass


def _number(tok: str, low: int, names: Optional[List[str]], fname: str) -> int:
    """cronie get_number(): a pure number, or a name -> index + low."""
    if tok.isdigit():
        return int(tok)
    if names is not None:
        for i, name in enumerate(names):
            if name == tok.lower():
                return i + low
    raise CronError(f"{fname}: bad value {tok!r}")


_ITEM_RE = re.compile(r"^([A-Za-z0-9]+)(?:-([A-Za-z0-9]+))?(?:/([0-9]+))?$")


def _parse_range(item: str, low: int, high: int, names: Optional[List[str]],
                 fname: str) -> Tuple[List[int], Optional[str]]:
    """One list item, mirroring cronie get_range(). Returns (values, warning)."""
    if item.startswith("*"):
        rest = item[1:]
        lo, hi, step = low, high, 1
        if rest:
            if not rest.startswith("/") or not rest[1:].isdigit():
                raise CronError(f"{fname}: bad item {item!r}")
            step = int(rest[1:])
            if step == 0:
                raise CronError(f"{fname}: step cannot be 0 in {item!r}")
    else:
        if "~" in item:
            raise CronError(
                f"{fname}: '~' random ranges are a cronie extension that picks a "
                "random value at parse time; not supported (pin a value instead)")
        m = _ITEM_RE.match(item)
        if not m:
            raise CronError(f"{fname}: bad item {item!r} "
                            "(note: steps need a range, e.g. '0-59/5', not '0/5')")
        lo = _number(m.group(1), low, names, fname)
        hi = lo
        step = 1
        if m.group(2) is not None:
            hi = _number(m.group(2), low, names, fname)
        if m.group(3) is not None:
            if m.group(2) is None:
                # cronie: a bare number followed by '/' is a parse error;
                # steps attach to a range or '*', never to one number.
                raise CronError(f"{fname}: bad item {item!r} "
                                "(steps need a range, e.g. '0-59/5', not '0/5')")
            step = int(m.group(3))
            if step == 0:
                raise CronError(f"{fname}: step cannot be 0 in {item!r}")
        if lo > hi and hi == 0:
            hi = 7  # cronie's Sunday-wrap hack, e.g. dow range '5-0' -> 5-7
    warning = None
    if lo > hi:
        # cronie does NOT error here: the bit-setting loop simply never runs.
        warning = (f"{fname}: range {item!r} has low > high; cron silently "
                   "accepts it and it matches nothing")
        return [], warning
    if step > 1 and step > hi - lo:
        warning = (f"{fname}: step {step} is larger than the range "
                   f"{lo}-{hi}; only {lo} matches (cronie prints a warning "
                   "and keeps it)")
    values = []
    for v in range(lo, hi + 1, step):
        if v < low or v > high:  # cronie set_element() bounds check
            raise CronError(f"{fname}: value {v} out of range {low}-{high}")
        values.append(v)
    return values, warning


def parse_field(spec: str, low: int, high: int, names: Optional[List[str]],
                fname: str) -> Tuple[frozenset, bool, List[str]]:
    """Parse one field. Returns (value set, star_flag, warnings).

    star_flag mirrors cronie: set when the field's FIRST character is '*',
    so '*/2' counts as unrestricted for the day-field OR rule."""
    if spec == "":
        raise CronError(f"{fname}: empty field")
    star = spec[0] == "*"
    values: set = set()
    warnings: List[str] = []
    for item in spec.split(","):
        vals, warn = _parse_range(item, low, high, names, fname)
        values.update(vals)
        if warn:
            warnings.append(warn)
    return frozenset(values), star, warnings


@dataclass(frozen=True)
class CronExpression:
    minute: frozenset
    hour: frozenset
    dom: frozenset
    month: frozenset
    dow: frozenset  # normalized: 0 (Sunday) present iff 7 present
    dom_star: bool
    dow_star: bool
    original: str
    reboot: bool = False
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def matches(self, dt: datetime) -> bool:
        """cronie's cron.c match: minute/hour/month must match; the two day
        fields OR only when neither had the STAR flag."""
        if self.reboot:
            return False
        if dt.minute not in self.minute or dt.hour not in self.hour \
                or dt.month not in self.month:
            return False
        cron_dow = (dt.weekday() + 1) % 7  # python Mon=0 -> cron Sun=0
        dom_hit = dt.day in self.dom
        dow_hit = cron_dow in self.dow
        if self.dom_star or self.dow_star:
            return dom_hit and dow_hit
        return dom_hit or dow_hit

    def next_after(self, dt: datetime, count: int = 1) -> List[datetime]:
        """The next fire times strictly after dt (minute precision)."""
        if self.reboot:
            return []
        out: List[datetime] = []
        start = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = start.date()
        hours = sorted(self.hour)
        minutes = sorted(self.minute)
        for day_i in range(SEARCH_DAYS):
            d = day + timedelta(days=day_i)
            if d.month in self.month and self._day_matches(d):
                for h in hours:
                    if day_i == 0 and h < start.hour:
                        continue
                    for m in minutes:
                        if day_i == 0 and h == start.hour and m < start.minute:
                            continue
                        out.append(datetime(d.year, d.month, d.day, h, m))
                        if len(out) == count:
                            return out
        return out

    def _day_matches(self, d) -> bool:
        cron_dow = (d.weekday() + 1) % 7
        dom_hit = d.day in self.dom
        dow_hit = cron_dow in self.dow
        if self.dom_star or self.dow_star:
            return dom_hit and dow_hit
        return dom_hit or dow_hit

    def explain(self) -> str:
        if self.reboot:
            return "@reboot: runs once when the cron daemon starts; no schedule."
        parts = [self._explain_time()]
        day = self._explain_day()
        if day:
            parts.append(day)
        text = ", ".join(parts) + "."
        if not self.dom_star and not self.dow_star:
            text += (" Note: BOTH day fields are restricted, so cron fires "
                     "when EITHER matches (OR), not only when both do.")
        return text

    def _explain_time(self) -> str:
        mins, hours = sorted(self.minute), sorted(self.hour)
        if len(mins) == 1 and len(hours) == 1:
            return f"at {hours[0]:02d}:{mins[0]:02d}"
        if len(mins) == len(MINUTE_RANGE):
            t = "every minute"
        else:
            t = f"at minute {_join(mins)}"
        if len(hours) == 1:
            t += f" past hour {hours[0]}" if len(mins) > 1 else ""
        elif len(hours) < 24:
            t += f" during hour {_join(hours)}"
        return t

    def _explain_day(self) -> str:
        out = []
        if not self.dom_star:
            out.append(f"on day-of-month {_join(sorted(self.dom))}")
        if not self.dow_star:
            out.append("on " + _join(sorted({d % 7 for d in self.dow}), DOW_FULL))
        months = sorted(self.month)
        if len(months) < 12:
            out.append("in " + _join([m - 1 for m in months], MONTH_FULL))
        return ", ".join(out)


MINUTE_RANGE = range(0, 60)
MONTH_FULL = ["January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
DOW_FULL = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday"]

# max day-of-month a schedule can hit in some year (Feb=29: leap years exist)
_MONTH_MAX = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
              7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _join(nums: List[int], names: Optional[List[str]] = None) -> str:
    def one(v):
        return names[v] if names else str(v)
    # collapse consecutive runs for readability
    runs: List[str] = []
    i = 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        if j > i + 1:
            runs.append(f"{one(nums[i])} through {one(nums[j])}")
        else:
            runs.extend(one(v) for v in nums[i:j + 1])
        i = j + 1
    if len(runs) > 2:
        return ", ".join(runs[:-1]) + " and " + runs[-1]
    return " and ".join(runs)


def parse(expr: str) -> CronExpression:
    """Parse a cron expression (5 fields, or @daily/@reboot/...)."""
    original = expr.strip()
    if not original:
        raise CronError("empty expression")
    if original.startswith("@"):
        word = original.split()[0].lower()
        if word == "@reboot":
            return CronExpression(frozenset(), frozenset(), frozenset(),
                                  frozenset(), frozenset(), False, False,
                                  original, reboot=True)
        if word in AT_FORMS:
            base = parse(AT_FORMS[word])
            return CronExpression(base.minute, base.hour, base.dom, base.month,
                                  base.dow, base.dom_star, base.dow_star,
                                  original, warnings=base.warnings)
        raise CronError(f"unknown @-form {word!r}; supported: @reboot, " +
                        ", ".join(sorted(AT_FORMS)))
    fields = original.split()
    if len(fields) != 5:
        raise CronError(f"expected 5 fields (minute hour day-of-month month "
                        f"day-of-week), got {len(fields)}; this tool uses "
                        "classic cron syntax, no seconds field (Quartz/systemd differ)")
    (mins, _ms, w1) = parse_field(fields[0], *MINUTE[1:], None, MINUTE[0])
    (hours, _hs, w2) = parse_field(fields[1], *HOUR[1:], None, HOUR[0])
    (dom, dom_star, w3) = parse_field(fields[2], *DOM[1:], None, DOM[0])
    (month, _mos, w4) = parse_field(fields[3], *MONTH[1:], MONTH_NAMES, MONTH[0])
    (dow, dow_star, w5) = parse_field(fields[4], *DOW[1:], DOW_NAMES, DOW[0])
    # cronie entry.c: "make sundays equivalent" - bits 0 and 7 set together
    if 0 in dow or 7 in dow:
        dow = frozenset(set(dow) | {0, 7})
    return CronExpression(mins, hours, dom, month, dow, dom_star, dow_star,
                          original, warnings=tuple(w1 + w2 + w3 + w4 + w5))


# ---------------------------------------------------------------------------
# Audit: expressions that don't do what their author almost certainly meant.
# ---------------------------------------------------------------------------

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    message: str


def audit(expr: CronExpression) -> List[Finding]:
    findings: List[Finding] = []
    if expr.reboot:
        findings.append(Finding("reboot", SEVERITY_INFO,
                                "@reboot has no schedule; it runs once when the "
                                "cron daemon (re)starts"))
        return findings
    for w in expr.warnings:
        if "low > high" in w:
            findings.append(Finding("empty-range", SEVERITY_WARNING, w))
        else:
            findings.append(Finding("step-oversize", SEVERITY_INFO, w))
    full_minute = len(expr.minute) == 60
    if full_minute:
        findings.append(Finding("every-minute", SEVERITY_INFO,
                                "fires every minute (1440 times per day)"))

    # Day-field reachability, following cron's OR/AND rule.
    dom_possible = any(d <= _MONTH_MAX[m] for m in expr.month for d in expr.dom)
    dow_possible = len(expr.dow) > 0
    if expr.dom_star or expr.dow_star:
        # AND: both bit tests must pass; a '*' side matches every day.
        can_fire = (expr.dom_star or dom_possible) and \
                   (expr.dow_star or dow_possible) and \
                   len(expr.minute) > 0 and len(expr.hour) > 0 and len(expr.month) > 0
        dom_must_match = not expr.dom_star
    else:
        can_fire = ((dom_possible and len(expr.dom) > 0) or dow_possible) and \
                   len(expr.minute) > 0 and len(expr.hour) > 0 and len(expr.month) > 0
        dom_must_match = False
    if not can_fire:
        findings.append(Finding(
            "never-fires", SEVERITY_ERROR,
            "this expression can never match any date or time "
            "(e.g. day 31 in a 30-day month, or day 30+ in February)"))
    else:
        viable = [(m, d) for m in expr.month for d in expr.dom
                  if d <= _MONTH_MAX[m]]
        if (not expr.dom_star and expr.dow_star and viable
                and all(m == 2 and d == 29 for m, d in viable)):
            # AND path: day-of-month must match, and only Feb 29 provides it
            findings.append(Finding(
                "leap-only", SEVERITY_INFO,
                "fires only on February 29, i.e. only in leap years "
                "(next: 2028, 2032, ...)"))

    if not expr.dom_star and not expr.dow_star and expr.dom and expr.dow:
        findings.append(Finding(
            "dom-dow-or", SEVERITY_WARNING,
            "both day fields are restricted: cron fires when EITHER matches "
            "(OR), not only when both do. '30 4 1,15 * 5' runs on the 1st and "
            "15th AND every Friday (crontab(5)). systemd timers do the "
            "opposite (they AND the weekday)"))
    for label, star, values, full in (
            ("day-of-month", expr.dom_star, expr.dom, frozenset(range(1, 32))),
            ("day-of-week", expr.dow_star, expr.dow, frozenset(range(0, 8)))):
        other_restricted = (not expr.dow_star) if label == "day-of-month" \
            else (not expr.dom_star)
        if star and values != full and other_restricted:
            findings.append(Finding(
                "star-step-day", SEVERITY_WARNING,
                f"{label} is written with '*/n', which STARTS with '*': cron "
                "treats the field as unrestricted for the OR rule, so the two "
                "day fields AND instead of OR (cronie entry.c). Write the "
                "range out (e.g. '1-31/2') to get OR semantics"))
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _finding_json(f: Finding) -> dict:
    return {"kind": f.kind, "severity": f.severity, "message": f.message}


def _parse_from(text: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise SystemExit(f"error: --from must look like '2026-10-18 09:30', got {text!r}")


def cmd_next(args: argparse.Namespace) -> int:
    expr = parse(args.expression)
    start = _parse_from(args.__dict__["from"]) if args.__dict__["from"] \
        else datetime.now()
    fires = expr.next_after(start, args.count)
    if args.json:
        print(json.dumps([f.strftime("%Y-%m-%d %H:%M") for f in fires], indent=2))
    else:
        for f in fires:
            print(f.strftime("%a %Y-%m-%d %H:%M"))
    if not fires:
        print("no fire time found within 8 years (expression likely never "
              "fires)", file=sys.stderr)
        return 1
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    expr = parse(args.expression)
    if args.json:
        print(json.dumps({"expression": expr.original,
                          "description": expr.explain(),
                          "warnings": list(expr.warnings)}, indent=2))
    else:
        print(expr.explain())
        for w in expr.warnings:
            print(f"warning: {w}", file=sys.stderr)
    return 0


def _audit_one(expression: str, label: Optional[str] = None) -> List[dict]:
    try:
        expr = parse(expression)
        return [dict(_finding_json(f), expression=expression, **({"line": label} if label else {}))
                for f in audit(expr)]
    except CronError as e:
        return [dict({"kind": "parse-error", "severity": SEVERITY_ERROR,
                      "message": str(e)}, expression=expression,
                     **({"line": label} if label else {}))]


def cmd_audit(args: argparse.Namespace) -> int:
    results: List[dict] = []
    if args.crontab:
        with open(args.crontab, "r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("@"):
                    schedule = line.split()[0]
                elif re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", line):
                    continue  # environment assignment (SHELL=, MAILTO=, ...)
                else:
                    schedule = " ".join(line.split()[:5])
                results.extend(_audit_one(schedule, str(lineno)))
    else:
        results = _audit_one(args.expression)
    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            loc = f"line {r['line']}: " if "line" in r else ""
            print(f"{loc}[{r['kind']}] {r['message']}  ({r['expression']})")
        if not results:
            print("no findings")
    return 1 if any(r["severity"] != SEVERITY_INFO for r in results) else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cron-check",
        description="Parse, explain, and audit cron expressions the way cron "
                    "actually runs them.")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("next", help="print the next fire times")
    p.add_argument("expression")
    p.add_argument("-n", "--count", type=int, default=5)
    p.add_argument("--from", help="start time, e.g. '2026-10-18 09:30'")
    p.set_defaults(func=cmd_next)
    p = sub.add_parser("explain", help="describe the schedule in words")
    p.add_argument("expression")
    p.set_defaults(func=cmd_explain)
    p = sub.add_parser("audit", help="flag expressions that won't do what you mean")
    p.add_argument("expression", nargs="?", default=None)
    p.add_argument("--crontab", help="audit every line of a crontab file")
    p.set_defaults(func=cmd_audit)
    args = parser.parse_args(argv)
    if args.command == "audit" and not args.crontab and not args.expression:
        print("error: audit needs an expression or --crontab FILE", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except CronError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
