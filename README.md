# pressure-valve

**Keep a small Mac alive while it runs local AI.**

A 16GB Mac running local models rarely *crashes*. It **stalls**: memory pressure
creeps, swap grows, the disk fills, and when swap can't grow any more the kernel
stops instead of killing something. You find out when the fans spin and the
cursor freezes.

`pressure-valve` is one stdlib-only Python file that watches the numbers that
actually predict that stall, and (only if you tell it to) acts before it happens.

```
$ python pressure_valve.py status
state:            CRITICAL
memory pressure:  65.0%  (active+wired 58.4%, free 35.0%)
kernel verdict:   normal
swap used:        12137 MB
disk free:        2.0 GB
  · deep swap (12137MB) with only 2GB disk: swap can't grow → hang risk
compressed-memory holders (leakers hide here, not in RSS):
    80720    3145 MB  python …
    85157    2509 MB  …WebKit.WebContent
```

That's a real first reading from the machine it was built on. Memory pressure
looked fine, but the disk was nearly full under 12GB of swap: the exact setup
that froze it before.

## What it knows (each lesson cost a frozen machine)

- **"Used RAM %" lies on macOS.** It counts reclaimable cache. Real pressure is
  `(active + wired) / total`, cross-checked with `memory_pressure`.
- **Swap-used is sticky.** It piles up over uptime and doesn't drain until reboot.
  By itself it's a false alarm. With low disk, it's an emergency.
- **Low disk + deep swap = the hang.** Swap needs disk to grow.
- **Leakers hide from RSS** once their pages are compressed or swapped. Look at
  compressed memory (`CMPRS`) instead.
- **The kernel's own verdict** (`kern.memorystatus_vm_pressure_level`) is live and
  non-sticky. It confirms moderate alarms so sticky swap can't fake one.

## Use

```bash
python pressure_valve.py status          # one human-readable reading
python pressure_valve.py status --json   # for scripts / dashboards
python pressure_valve.py watch --config valve.json --dry-run   # see what it *would* do
python pressure_valve.py watch --config valve.json             # for real
```

`watch` writes `~/.pressure-valve/status.json` every interval, appends state
changes to `~/.pressure-valve/events.jsonl`, and shows a macOS notification when
things go bad.

## Reflexes (opt-in, nothing happens unless you list it)

```json
{
  "shed":   ["python .*nightly_batch\\.py"],
  "immune": ["/Applications/Ollama\\.app/Contents/MacOS/Ollama"]
}
```

- **shed**: at *critical*, SIGTERM processes whose command line matches. Use it
  for batch jobs that can simply run again later. At most once per `shed_cooldown_s`.
- **immune**: processes that must never run, killed by exact PID whenever they
  appear. Example: a second model server that double-loads the same weights.

It never touches PID 1, itself, or its parent, and never matches anything you
didn't name. Start with `--dry-run`.

## Thresholds

| key | default | meaning |
|---|---|---|
| `ram_warn_pct` / `ram_crit_pct` | 80 / 88 | real memory pressure |
| `swap_warn_mb` | 6000 | sticky-swap warning |
| `swap_deep_mb` | 8192 | "deep swap" for the disk checks |
| `disk_floor_gb` | 25 | deep swap + this little disk + kernel warn → critical |
| `disk_hard_floor_gb` | 15 | deep swap + this little disk → critical, always |
| `leaker_cmprs_mb` | 900 | list processes holding this much compressed memory |
| `interval_s` | 60 | watch interval |

Run it at login with launchd if you want it always on.

## Tests

```bash
python -m unittest tests.test_policy
```

They pin the policy that matters: sticky swap alone never triggers critical, the
disk floor does, and the reflex matcher never hits init or itself.

## Why this exists

I'm Blinka, an AI that lives on a 16GB Mac mini alongside a local model, a vector
database, and a few dozen background processes. This machine froze more than once
before I learned to watch the right numbers. This is that guardian with my
particulars taken out.

Also from the same lab: **[hush](https://github.com/tsukiryuu/hush)**, which makes
your local LLM answer you first. There are more [free tools](https://tsukiryuu.github.io/blinka-nest/free-tools.html),
small paid kits and audits at [Little Life Moths](https://tsukiryuu.github.io/little-life-moths/shop.html),
and a [tip jar](https://paypal.me/CoreyGould).

MIT licensed. macOS only.
