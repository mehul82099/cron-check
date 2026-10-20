# cron-check

Parse, explain, and audit cron expressions the way cron **actually runs them** — not the way they look like they should run.

Cron has three famous traps, and this tool catches all of them before they catch you:

**1. The day fields OR, they don't AND.** When both `day-of-month` and `day-of-week` are restricted, cron fires when **either** matches:

```console
$ cron-check explain '30 4 1,15 * 5'
at 04:30, on day-of-month 1 and 15, on Friday. Note: BOTH day fields are restricted, so cron fires when EITHER matches (OR), not only when both do.
```

**2. `*/2` is not "restricted".** In Vixie cron the field's STAR flag depends only on its *first character*. `0 0 */2 * 1` therefore ANDs the day fields (Mondays that fall on odd days), while `0 0 1-31/2 * 1` ORs them (every Monday *and* every odd day) — the opposite of what the two expressions look like:

```console
$ cron-check next '0 0 */2 * 1' --from '2026-10-18' -n 3
Mon 2026-10-19 00:00
Mon 2026-11-09 00:00
Mon 2026-11-23 00:00
$ cron-check audit '0 0 */2 * 1'
[star-step-day] day-of-month is written with '*/n', which STARTS with '*': cron treats the field as unrestricted for the OR rule, so the two day fields AND instead of OR (cronie entry.c). Write the range out (e.g. '1-31/2') to get OR semantics
```

**3. Silently dead schedules.** `0 0 31 4 *` (April has 30 days) and `5-1 * * * *` (a reversed range, which cron *silently accepts* as matching nothing) never fire, ever:

```console
$ cron-check audit '0 0 31 4 *'
[never-fires] this expression can never match any date or time (e.g. day 31 in a 30-day month, or day 30+ in February)
```

## What it does

- **`next 'EXPR' [-n N] [--from '...']`** — the next N fire times (strictly after the start; minute precision).
- **`explain 'EXPR'`** — the schedule in plain words, including the OR-rule note.
- **`audit 'EXPR'`** — findings: `never-fires`, `leap-only` (Feb 29 schedules), `dom-dow-or`, `star-step-day`, `empty-range`, `step-oversize`, `every-minute`, `reboot`.
- **`audit --crontab FILE`** — audits every line of a real crontab (skips comments and `NAME=value` environment lines, handles `@reboot`).
- Supports the classic 5-field grammar plus `@hourly/@daily/@midnight/@weekly/@monthly/@yearly/@annually/@reboot`; names (`jan-mar`, `mon,wed,fri`) are case-insensitive; day-of-week `0` and `7` are both Sunday. Python 3.9+, standard library only, no network. `--json` everywhere.

## Faithfulness, and how it is tested

The implementation mirrors cron's own source, and the tests prove the semantics three ways (57 tests):

1. **The manual's worked examples, verbatim.** Every example from the [`crontab(5)` man page](https://man7.org/linux/man-pages/man5/crontab.5.html) — including `30 4 1,15 * 5` ("4:30 am on the 1st and 15th, *plus* every Friday") and the NOTES-section trap that `*/35` minutes means *0 and 35 past the hour*, twice an hour, not "every 35 minutes".
2. **Source-level pins of [cronie](https://github.com/cronie-crond/cronie) behavior** (the maintained Vixie cron that ships on most Linux systems): parsing follows [`src/entry.c`](https://github.com/cronie-crond/cronie/blob/master/src/entry.c) (`get_range`/`get_number`: the `low > high && high == 0 -> high = 7` Sunday-wrap hack, reversed ranges silently matching nothing, step-larger-than-range being a *warning* not an error, `'0/5'` being a parse error because steps attach only to ranges or `*`), and matching follows [`src/cron.c`](https://github.com/cronie-crond/cronie/blob/master/src/cron.c) (`(DOM_STAR || DOW_STAR) ? dom && dow : dom || dow`) — the code that makes `*/2` count as unrestricted.
3. **A randomized differential test.** The fast next-fire navigation is checked against an independent brute-force minute-by-minute scanner (with its own integer date arithmetic) on 400 seeded random expressions — 400 expressions x a 45-day window each.

## Scope and limitations

- Classic 5-field cron only. Quartz adds a seconds field (and `?`); systemd timers use a different calendar syntax that **ANDs** the weekday with the other fields — the opposite of cron's OR ([`systemd.time(7)`](https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html)). The parser rejects both on sight.
- cronie's `~` random ranges are intentionally unsupported: they pick a random value *at parse time*, so there is no schedule to explain (pin a value instead).
- Times are naive local wall-clock times, like cron itself. Around DST transitions cron skips the missing hour and runs matching jobs twice in the repeated hour (crontab(5)); this tool neither models nor adjusts for that. If your scheduler is **GitHub Actions**, scheduled workflows [run in UTC by default](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule) (with an optional IANA timezone) — compute your times in UTC.
- The audit's `never-fires` and `leap-only` checks are calendar proofs, not searches; `star-step-day` and `dom-dow-or` state cronie's exact rule, not a guess.

## License

MIT — see [LICENSE](LICENSE).
