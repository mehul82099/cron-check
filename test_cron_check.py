"""Tests for cron_check.

Layers:
- the worked examples from the crontab(5) man page, verbatim;
- pins of cronie (Vixie cron) source behavior from src/entry.c and
  src/cron.c: STAR-flag semantics of '*/n', the Sunday 0/7 equivalence,
  the '5-0' -> '5-7' wrap hack, silently-empty reversed ranges;
- a randomized differential test: the fast next-fire navigation must agree
  with an independent brute-force minute-by-minute scanner (with its own
  date arithmetic) on hundreds of seeded random expressions;
- audit and CLI tests.
"""

import calendar
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cron_check as cc

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "cron_check.py")


def nxt(expr, y, m, d, hh=0, mm=0, count=1):
    return cc.parse(expr).next_after(datetime(y, m, d, hh, mm), count)


def stamps(fires):
    return [f.strftime("%Y-%m-%d %H:%M") for f in fires]


class TestFieldParsing(unittest.TestCase):
    def field(self, spec, low=0, high=59, names=None):
        return cc.parse_field(spec, low, high, names, "minute")

    def test_star_is_full_range(self):
        values, star, _ = self.field("*")
        self.assertEqual(values, frozenset(range(60)))
        self.assertTrue(star)

    def test_single_number(self):
        values, star, _ = self.field("5")
        self.assertEqual(values, frozenset({5}))
        self.assertFalse(star)

    def test_list_and_range(self):
        values, _, _ = self.field("1,2,5,9")
        self.assertEqual(values, frozenset({1, 2, 5, 9}))
        values, _, _ = self.field("0-4,8-12")
        self.assertEqual(values, frozenset(set(range(0, 5)) | set(range(8, 13))))

    def test_steps_stay_within_the_field(self):
        # crontab(5): "*/23" in the hours field means hours 0 and 23.
        values, _, _ = cc.parse_field("*/23", 0, 23, None, "hour")
        self.assertEqual(values, frozenset({0, 23}))
        # crontab(5) NOTES: "*/35" minutes -> 0 and 35, not "every 35 min".
        values, _, _ = cc.parse_field("*/35", 0, 59, None, "minute")
        self.assertEqual(values, frozenset({0, 35}))

    def test_step_with_range(self):
        values, _, _ = self.field("0-23/2")
        self.assertEqual(values, frozenset(range(0, 24, 2)))

    def test_names_case_insensitive(self):
        values, _, _ = cc.parse_field("Mon,wed,FRI", 0, 7, cc.DOW_NAMES, "dow")
        self.assertEqual(values, frozenset({1, 3, 5}))
        values, _, _ = cc.parse_field("jan-MAR", 1, 12, cc.MONTH_NAMES, "month")
        self.assertEqual(values, frozenset({1, 2, 3}))

    def test_star_flag_depends_only_on_first_character(self):
        # cronie entry.c: the STAR flag is set when the first char is '*',
        # so '*/2' counts as UNRESTRICTED for the day-field OR rule.
        _, star, _ = cc.parse_field("*/2", 1, 31, None, "dom")
        self.assertTrue(star)
        _, star, _ = cc.parse_field("1-31/2", 1, 31, None, "dom")
        self.assertFalse(star)

    def test_dow_wrap_hack(self):
        # cronie get_range: low > high && high == 0 -> high = 7 ('5-0' = 5-7)
        values, _, _ = cc.parse_field("5-0", 0, 7, cc.DOW_NAMES, "dow")
        self.assertEqual(values, frozenset({5, 6, 7}))

    def test_reversed_range_is_silently_empty_not_an_error(self):
        # cronie does not error on '5-1': the bit loop never runs. Pin it.
        values, _, warnings = cc.parse_field("5-1", 0, 59, None, "minute")
        self.assertEqual(values, frozenset())
        self.assertEqual(len(warnings), 1)
        self.assertIn("matches nothing", warnings[0])

    def test_out_of_range_values_rejected(self):
        for bad, low, high, names in (
                ("60", 0, 59, None), ("24", 0, 23, None), ("0", 1, 31, None),
                ("32", 1, 31, None), ("13", 1, 12, cc.MONTH_NAMES),
                ("8", 0, 7, cc.DOW_NAMES)):
            with self.assertRaises(cc.CronError, msg=bad):
                cc.parse_field(bad, low, high, names, "field")

    def test_step_needs_a_range(self):
        # cronie: '0/5' is a parse error (steps only after * or a range).
        with self.assertRaises(cc.CronError):
            self.field("0/5")

    def test_step_zero_rejected(self):
        with self.assertRaises(cc.CronError):
            self.field("*/0")
        with self.assertRaises(cc.CronError):
            self.field("1-10/0")

    def test_tilde_random_ranges_unsupported(self):
        with self.assertRaises(cc.CronError) as ctx:
            self.field("6~15")
        self.assertIn("~", str(ctx.exception))

    def test_junk_rejected(self):
        for bad in ("", "x", "5x", "janx", "5-", "-5", "5--6", "1,2,,3", "*/",
                    "1/2", "funday"):
            with self.assertRaises(cc.CronError, msg=repr(bad)):
                self.field(bad)

    def test_step_oversize_warns_but_keeps_first(self):
        values, _, warnings = self.field("5-6/10")
        self.assertEqual(values, frozenset({5}))
        self.assertEqual(len(warnings), 1)


class TestSundayEquivalence(unittest.TestCase):
    def test_0_and_7_are_both_sunday(self):
        e0 = cc.parse("0 9 * * 0")
        e7 = cc.parse("0 9 * * 7")
        esun = cc.parse("0 9 * * sun")
        self.assertEqual(e0.dow, e7.dow)
        self.assertEqual(e0.dow, esun.dow)
        # 2026-10-18 is a Sunday
        self.assertTrue(e0.matches(datetime(2026, 10, 18, 9, 0)))
        self.assertFalse(e0.matches(datetime(2026, 10, 19, 9, 0)))

    def test_weekend_range_6_0_via_wrap(self):
        e = cc.parse("0 9 * * 6-0")  # hack: 6-0 -> 6-7 = Sat,Sun
        self.assertTrue(e.matches(datetime(2026, 10, 17, 9, 0)))  # Sat
        self.assertTrue(e.matches(datetime(2026, 10, 18, 9, 0)))  # Sun
        self.assertFalse(e.matches(datetime(2026, 10, 19, 9, 0)))  # Mon


class TestManPageExamples(unittest.TestCase):
    """Every worked example in crontab(5), verbatim comments included."""

    def test_five_past_midnight_daily(self):
        # 5 0 * * *  -- run five minutes after midnight, every day
        self.assertEqual(stamps(nxt("5 0 * * *", 2026, 10, 18, 12, 0)),
                         ["2026-10-19 00:05"])

    def test_monthly_215pm_on_the_first(self):
        # 15 14 1 * *  -- run at 2:15pm on the first of every month
        self.assertEqual(stamps(nxt("15 14 1 * *", 2026, 10, 18)),
                         ["2026-11-01 14:15"])

    def test_10pm_on_weekdays(self):
        # 0 22 * * 1-5  -- run at 10 pm on weekdays
        self.assertEqual(stamps(nxt("0 22 * * 1-5", 2026, 10, 16, 23, 0)),  # Fri
                         ["2026-10-19 22:00"])  # Monday

    def test_every_two_hours_at_23_past(self):
        # 23 0-23/2 * * *  -- 23 minutes after midn, 2am, 4am ..., everyday
        fires = nxt("23 0-23/2 * * *", 2026, 10, 18, 0, 0, count=4)
        self.assertEqual(stamps(fires), ["2026-10-18 00:23", "2026-10-18 02:23",
                                         "2026-10-18 04:23", "2026-10-18 06:23"])
        self.assertEqual(len(nxt("23 0-23/2 * * *", 2026, 10, 18, 0, 0, count=12)), 12)

    def test_sunday_5_after_4(self):
        # 5 4 * * sun  -- run at 5 after 4 every sunday
        # 2026-10-18 04:06 is just past this Sunday's fire -> next Sunday
        self.assertEqual(stamps(nxt("5 4 * * sun", 2026, 10, 18, 4, 6)),
                         ["2026-10-25 04:05"])

    def test_day_of_month_or_day_of_week(self):
        # 30 4 1,15 * 5  -- 4:30 am on the 1st and 15th, PLUS every Friday
        e = cc.parse("30 4 1,15 * 5")
        self.assertTrue(e.matches(datetime(2026, 10, 16, 4, 30)))   # Friday
        self.assertTrue(e.matches(datetime(2026, 11, 1, 4, 30)))    # 1st (Sunday)
        self.assertTrue(e.matches(datetime(2026, 10, 15, 4, 30)))   # 15th (Thu)
        self.assertFalse(e.matches(datetime(2026, 10, 14, 4, 30)))  # neither
        fires = stamps(e.next_after(datetime(2026, 10, 18), 3))
        self.assertEqual(fires, ["2026-10-23 04:30", "2026-10-30 04:30",
                                 "2026-11-01 04:30"])


class TestStarStepSemantics(unittest.TestCase):
    """The cronie subtlety: '*/n' sets the STAR flag, flipping OR to AND."""

    def test_star_step_counts_as_unrestricted(self):
        # '0 0 */2 * 1': dom field starts with '*' -> STAR flag -> AND:
        # fires only on Mondays that ALSO fall on odd days of the month.
        fires = stamps(nxt("0 0 */2 * 1", 2026, 10, 18, count=3))
        for f in fires:
            dt = datetime.strptime(f, "%Y-%m-%d %H:%M")
            self.assertEqual(dt.weekday(), 0)      # Monday
            self.assertEqual(dt.day % 2, 1)        # odd day-of-month
        self.assertEqual(fires, ["2026-10-19 00:00", "2026-11-09 00:00",
                                 "2026-11-23 00:00"])

    def test_written_out_range_gets_or_semantics(self):
        # '0 0 1-31/2 * 1': no leading '*' -> restricted -> OR:
        # fires on Mondays OR odd days, whichever comes first.
        fires = stamps(nxt("0 0 1-31/2 * 1", 2026, 10, 18, count=3))
        self.assertEqual(fires, ["2026-10-19 00:00", "2026-10-21 00:00",
                                 "2026-10-23 00:00"])


class TestMatchingAndNext(unittest.TestCase):
    def test_strictly_after(self):
        fires = nxt("0 9 * * *", 2026, 10, 18, 9, 0)
        self.assertEqual(stamps(fires), ["2026-10-19 09:00"])

    def test_seconds_are_truncated_up(self):
        fires = cc.parse("0 9 * * *").next_after(datetime(2026, 10, 18, 8, 59, 59))
        self.assertEqual(stamps(fires), ["2026-10-18 09:00"])

    def test_leap_day_schedule(self):
        self.assertEqual(stamps(nxt("0 0 29 2 *", 2026, 1, 1)), ["2028-02-29 00:00"])
        self.assertEqual(stamps(nxt("0 0 29 2 *", 2028, 3, 1)), ["2032-02-29 00:00"])

    def test_year_rollover(self):
        self.assertEqual(stamps(nxt("0 0 1 1 *", 2026, 12, 31)), ["2027-01-01 00:00"])

    def test_every_minute(self):
        fires = nxt("* * * * *", 2026, 10, 18, 10, 30, count=2)
        self.assertEqual(stamps(fires), ["2026-10-18 10:31", "2026-10-18 10:32"])

    def test_never_firing_returns_empty(self):
        self.assertEqual(nxt("0 0 31 2 *", 2026, 1, 1), [])
        self.assertEqual(nxt("0 0 31 4 *", 2026, 1, 1), [])

    def test_reboot_has_no_fires(self):
        self.assertEqual(cc.parse("@reboot").next_after(datetime(2026, 1, 1)), [])
        self.assertFalse(cc.parse("@reboot").matches(datetime(2026, 1, 1)))

    def test_at_forms(self):
        self.assertEqual(stamps(nxt("@hourly", 2026, 10, 18, 10, 30)),
                         ["2026-10-18 11:00"])
        self.assertEqual(stamps(nxt("@daily", 2026, 10, 18, 10, 30)),
                         ["2026-10-19 00:00"])
        self.assertEqual(stamps(nxt("@weekly", 2026, 10, 18, 0, 1)),
                         ["2026-10-25 00:00"])
        self.assertEqual(stamps(nxt("@monthly", 2026, 10, 18)), ["2026-11-01 00:00"])
        self.assertEqual(stamps(nxt("@yearly", 2026, 10, 18)), ["2027-01-01 00:00"])
        self.assertEqual(cc.parse("@midnight").minute, cc.parse("@daily").minute)

    def test_bad_expression_errors(self):
        for bad in ("", "0 9 * *", "0 9 * * * *", "@fortnightly", "@"):
            with self.assertRaises(cc.CronError, msg=repr(bad)):
                cc.parse(bad)


class TestPropertyDifferential(unittest.TestCase):
    """Fast next-fire navigation vs an independent brute-force minute scan
    (own integer date arithmetic) on seeded random expressions."""

    WINDOW_DAYS = 45

    @staticmethod
    def brute_next(e, start, limit_days):
        cur = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        y, mo, d, hh, mm = cur.year, cur.month, cur.day, cur.hour, cur.minute
        wd = cur.weekday()
        for _ in range(limit_days * 24 * 60):
            cdow = (wd + 1) % 7
            if mm in e.minute and hh in e.hour and mo in e.month:
                dh, wh = d in e.dom, cdow in e.dow
                if (dh and wh) if (e.dom_star or e.dow_star) else (dh or wh):
                    return datetime(y, mo, d, hh, mm)
            mm += 1
            if mm == 60:
                mm = 0
                hh += 1
                if hh == 24:
                    hh = 0
                    d += 1
                    wd = (wd + 1) % 7
                    if d > calendar.monthrange(y, mo)[1]:
                        d = 1
                        mo += 1
                        if mo == 13:
                            mo = 1
                            y += 1
        return None

    @staticmethod
    def random_field(rng, low, high, names, allow_star=True):
        r = rng.random()
        if allow_star and r < 0.3:
            return "*"
        if allow_star and r < 0.45:
            return f"*/{rng.randint(2, max(2, (high - low) // 2))}"
        if r < 0.6:
            a = rng.randint(low, high)
            b = rng.randint(a, high)
            item = f"{a}-{b}"
            if rng.random() < 0.4:
                item += f"/{rng.randint(1, 5)}"
            return item
        k = rng.randint(1, 4)
        return ",".join(str(rng.randint(low, high)) for _ in range(k))

    def test_fast_next_agrees_with_brute_force(self):
        rng = random.Random(20261018)
        checked = 0
        for _ in range(400):
            expr = " ".join([
                self.random_field(rng, 0, 59, None),
                self.random_field(rng, 0, 23, None),
                self.random_field(rng, 1, 31, None),
                self.random_field(rng, 1, 12, cc.MONTH_NAMES),
                self.random_field(rng, 0, 7, cc.DOW_NAMES),
            ])
            try:
                e = cc.parse(expr)
            except cc.CronError:
                continue
            start = datetime(2026, 1, 1) + timedelta(
                minutes=rng.randint(0, 300 * 24 * 60))
            fast = e.next_after(start, 1)
            fast = fast[0] if fast else None
            brute = self.brute_next(e, start, self.WINDOW_DAYS)
            horizon = start + timedelta(days=self.WINDOW_DAYS)
            if brute is None:
                self.assertTrue(fast is None or fast >= horizon,
                                f"{expr!r}: fast found {fast} inside window")
            else:
                self.assertEqual(fast, brute, f"expression {expr!r}")
            checked += 1
        self.assertGreaterEqual(checked, 350)

    def test_next_is_strictly_increasing(self):
        e = cc.parse("*/17 5-22/3 1,15,29 * 1,4")
        t = datetime(2026, 1, 1)
        prev = None
        for f in e.next_after(t, 40):
            self.assertTrue(e.matches(f))
            if prev is not None:
                self.assertGreater(f, prev)
            prev = f


class TestAudit(unittest.TestCase):
    def kinds(self, expr):
        return [f.kind for f in cc.audit(cc.parse(expr))]

    def test_never_fires(self):
        self.assertIn("never-fires", self.kinds("0 0 31 2 *"))
        self.assertIn("never-fires", self.kinds("0 0 31 4 *"))
        self.assertIn("never-fires", self.kinds("0 0 31 4,6,9,11 *"))
        self.assertIn("never-fires", self.kinds("0 0 5-1 * *"))  # empty minutes? no: dom
        self.assertIn("never-fires", self.kinds("5-1 0 * * *"))  # empty minute field

    def test_not_never_when_dow_can_save_it(self):
        # OR path: day-of-month 31 in April never matches, but Friday does.
        self.assertNotIn("never-fires", self.kinds("0 0 31 4 5"))
        self.assertIn("dom-dow-or", self.kinds("0 0 31 4 5"))

    def test_leap_only(self):
        self.assertIn("leap-only", self.kinds("0 0 29 2 *"))
        self.assertIn("leap-only", self.kinds("0 0 29-31 2 *"))
        self.assertNotIn("leap-only", self.kinds("0 0 28,29 2 *"))
        self.assertNotIn("leap-only", self.kinds("0 0 29 2,3 *"))

    def test_dom_dow_or_warning(self):
        kinds = self.kinds("30 4 1,15 * 5")
        self.assertIn("dom-dow-or", kinds)
        self.assertNotIn("never-fires", kinds)

    def test_star_step_day_warning(self):
        self.assertIn("star-step-day", self.kinds("0 0 */2 * 1"))
        self.assertIn("star-step-day", self.kinds("0 0 1 * */2"))
        self.assertNotIn("star-step-day", self.kinds("0 0 */2 * *"))
        self.assertNotIn("star-step-day", self.kinds("0 0 1-31/2 * 1"))

    def test_every_minute_info(self):
        self.assertIn("every-minute", self.kinds("* * * * *"))
        self.assertNotIn("every-minute", self.kinds("*/2 * * * *"))

    def test_reboot_info(self):
        self.assertEqual(self.kinds("@reboot"), ["reboot"])

    def test_empty_range_and_step_oversize(self):
        self.assertIn("empty-range", self.kinds("0 0 * * 5-1"))
        self.assertIn("step-oversize", self.kinds("5-6/10 * * * *"))

    def test_clean_expression_has_no_warnings(self):
        findings = [f for f in cc.audit(cc.parse("0 9 * * 1-5"))
                    if f.severity != "info"]
        self.assertEqual(findings, [])


class TestExplain(unittest.TestCase):
    def test_exact_time(self):
        self.assertIn("at 04:30", cc.parse("30 4 * * *").explain())

    def test_or_note_present_only_when_both_restricted(self):
        self.assertIn("EITHER", cc.parse("30 4 1,15 * 5").explain())
        self.assertNotIn("EITHER", cc.parse("30 4 1,15 * *").explain())

    def test_weekdays_and_months(self):
        text = cc.parse("0 9 * 1-3 1-5").explain()
        self.assertIn("Monday through Friday", text)
        self.assertIn("January through March", text)

    def test_reboot(self):
        self.assertIn("no schedule", cc.parse("@reboot").explain())


class TestCLI(unittest.TestCase):
    def run_cli(self, *argv):
        return subprocess.run([sys.executable, SCRIPT, *argv],
                              capture_output=True, text=True, cwd=HERE)

    def test_next(self):
        r = self.run_cli("next", "5 0 * * *", "--from", "2026-10-18 12:00", "-n", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2026-10-19 00:05", r.stdout)
        self.assertIn("2026-10-20 00:05", r.stdout)

    def test_next_never_fires_exit_1(self):
        r = self.run_cli("next", "0 0 31 2 *", "--from", "2026-01-01")
        self.assertEqual(r.returncode, 1)
        self.assertIn("never", r.stderr)

    def test_explain(self):
        r = self.run_cli("explain", "0 22 * * 1-5")
        self.assertEqual(r.returncode, 0)
        self.assertIn("22:00", r.stdout)
        self.assertIn("Monday through Friday", r.stdout)

    def test_audit_exit_codes(self):
        self.assertEqual(self.run_cli("audit", "30 4 1,15 * 5").returncode, 1)
        self.assertEqual(self.run_cli("audit", "0 9 * * 1-5").returncode, 0)
        self.assertEqual(self.run_cli("audit", "0 0 31 2 *").returncode, 1)

    def test_audit_crontab_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cron", delete=False) as fh:
            fh.write("# comment\n")
            fh.write("SHELL=/bin/sh\n")
            fh.write("MAILTO=ops@example.com\n")
            fh.write("0 9 * * 1-5 /usr/bin/backup\n")
            fh.write("30 4 1,15 * 5 root /usr/bin/oddjob\n")
            fh.write("@reboot /usr/bin/startup\n")
            path = fh.name
        self.addCleanup(os.unlink, path)
        r = self.run_cli("audit", "--crontab", path)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("dom-dow-or", r.stdout)
        self.assertIn("line 5", r.stdout)
        self.assertNotIn("line 2", r.stdout)
        self.assertIn("reboot", r.stdout)

    def test_json_output(self):
        r = self.run_cli("--json", "next", "0 9 * * *", "--from", "2026-10-18", "-n", "2")
        # --from with no time means midnight; 09:00 the same day is ahead
        self.assertEqual(json.loads(r.stdout), ["2026-10-18 09:00",
                                                "2026-10-19 09:00"])
        r = self.run_cli("--json", "audit", "0 0 31 2 *")
        kinds = [f["kind"] for f in json.loads(r.stdout)]
        self.assertIn("never-fires", kinds)

    def test_parse_error_exit_2(self):
        self.assertEqual(self.run_cli("explain", "0 9 * *").returncode, 2)
        self.assertEqual(self.run_cli("next", "0/5 * * * *").returncode, 2)

    def test_audit_needs_input(self):
        self.assertEqual(self.run_cli("audit").returncode, 2)


if __name__ == "__main__":
    unittest.main()
