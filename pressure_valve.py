#!/usr/bin/env python3
"""pressure-valve — keep a small Mac alive while it runs local AI.

Local models on a 16GB Mac fail in boring, repeatable ways: memory pressure
creeps, swap grows, the disk fills, and one day the machine doesn't crash, it
just *stalls* (swap lives on disk; when it can't grow, the kernel stops instead
of killing something). pressure-valve watches the numbers that actually predict
that, and can act before it happens.

Lessons baked in (each one cost a frozen machine):
  * psutil's "used %" counts reclaimable cache. Real pressure is
    (active + wired) / total, cross-checked with `memory_pressure`.
  * swap-used is sticky: it accumulates over uptime and doesn't drain until
    reboot. Alone it's a false alarm; with low disk it's an emergency.
  * low disk + deep swap = the hang. Swap needs disk to grow.
  * leakers hide from RSS once they're compressed or swapped. Look at CMPRS.
  * the kernel's own verdict (kern.memorystatus_vm_pressure_level) is live
    and non-sticky; use it to confirm a moderate alarm.

Reflexes are opt-in from a JSON config and never touch anything not named there:
  * shed     — at critical pressure, SIGTERM processes matching these patterns
               (batch jobs that can simply re-run later).
  * immune   — processes that must never run (e.g. a second model server that
               double-loads weights); killed by exact PID whenever seen.
  * notify   — a macOS notification on each state change.

Usage:
  python pressure_valve.py status            # one reading, human-readable
  python pressure_valve.py status --json
  python pressure_valve.py watch [--config valve.json] [--dry-run]

stdlib only, macOS only. MIT license.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULTS = {
    "interval_s": 60,
    "ram_warn_pct": 80.0,
    "ram_crit_pct": 88.0,
    "swap_warn_mb": 6000,
    "swap_deep_mb": 8192,
    "disk_floor_gb": 25.0,        # with deep swap + kernel warn → critical
    "disk_hard_floor_gb": 15.0,   # with deep swap → critical, no questions asked
    "leaker_cmprs_mb": 900,       # report processes holding this much compressed memory
    "shed": [],                   # regex patterns matched against the full command line
    "immune": [],
    "notify": True,
    "shed_cooldown_s": 600,
    "status_file": "~/.pressure-valve/status.json",
    "log_file": "~/.pressure-valve/events.jsonl",
}


def _run(cmd: list[str], timeout: float = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


# ── readings ────────────────────────────────────────────────────────────────
def parse_vm_stat(text: str) -> tuple[int, dict[str, int]]:
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 16384
    pages = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            v = v.strip().rstrip(".")
            if v.isdigit():
                pages[k.strip()] = int(v)
    return page, pages


def parse_swap(text: str) -> float:
    m = re.search(r"used\s*=\s*([0-9.]+)M", text)
    return float(m.group(1)) if m else 0.0


def parse_top_cmprs(text: str, limit_mb: float) -> dict[int, float]:
    """PIDs whose compressed memory ≥ limit_mb, from `top -l 1 -stats pid,cmprs`."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].rstrip("*").isdigit():
            continue
        raw = parts[1].rstrip("+-")
        mult = {"K": 1 / 1024, "M": 1.0, "G": 1024.0}.get(raw[-1:], 1 / (1024 * 1024))
        try:
            mb = float(raw.rstrip("KMGB")) * mult
        except ValueError:
            continue
        if mb >= limit_mb:
            out[int(parts[0].rstrip("*"))] = round(mb)
    return out


@dataclass
class Reading:
    pressure_pct: float
    committed_pct: float
    free_pct: float | None
    kernel_level: int | None   # 1 normal, 2 warn, 4 critical
    swap_mb: float
    disk_free_gb: float
    leakers: dict = field(default_factory=dict)


def read(cfg: dict) -> Reading:
    page, pages = parse_vm_stat(_run(["vm_stat"]))
    total = int(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0)
    used = (pages.get("Pages active", 0) + pages.get("Pages wired down", 0)) * page
    committed = round(used / total * 100, 1) if total else 0.0
    m = re.search(r"System-wide memory free percentage:\s*(\d+)%", _run(["memory_pressure"], 15))
    free = float(m.group(1)) if m else None
    pressure = max(committed, 100.0 - free) if free is not None else committed
    lvl = _run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip()
    swap = parse_swap(_run(["sysctl", "-n", "vm.swapusage"]))
    disk = shutil.disk_usage("/System/Volumes/Data" if Path("/System/Volumes/Data").exists() else "/").free / 1024**3
    top = _run(["top", "-l", "1", "-stats", "pid,cmprs", "-o", "cmprs", "-n", "15"], 60)
    leak = parse_top_cmprs(top, cfg["leaker_cmprs_mb"])
    named = {}
    for pid, mb in leak.items():
        cmd = _run(["ps", "-o", "command=", "-p", str(pid)]).strip()
        named[str(pid)] = {"cmprs_mb": mb, "command": cmd[:160]}
    return Reading(round(pressure, 1), committed, free, int(lvl) if lvl.isdigit() else None,
                   round(swap), round(disk, 1), named)


# ── policy (pure, tested) ───────────────────────────────────────────────────
def assess(r: Reading, cfg: dict) -> tuple[str, list[str]]:
    """→ ("ok"|"warn"|"critical", reasons)."""
    why = []
    deep = r.swap_mb >= cfg["swap_deep_mb"]
    kernel_worried = r.kernel_level is None or r.kernel_level >= 2
    if deep and r.disk_free_gb < cfg["disk_hard_floor_gb"]:
        why.append(f"deep swap ({r.swap_mb:.0f}MB) with only {r.disk_free_gb:.0f}GB disk: swap can't grow → hang risk")
    elif deep and r.disk_free_gb < cfg["disk_floor_gb"] and kernel_worried:
        why.append(f"deep swap ({r.swap_mb:.0f}MB), {r.disk_free_gb:.0f}GB disk, kernel pressure {r.kernel_level}")
    if r.pressure_pct >= cfg["ram_crit_pct"]:
        why.append(f"memory pressure {r.pressure_pct}% ≥ {cfg['ram_crit_pct']}%")
    if r.kernel_level == 4:
        why.append("kernel reports CRITICAL memory pressure")
    if why:
        return "critical", why
    if r.pressure_pct >= cfg["ram_warn_pct"]:
        why.append(f"memory pressure {r.pressure_pct}%")
    if r.swap_mb >= cfg["swap_warn_mb"]:
        why.append(f"swap {r.swap_mb:.0f}MB (sticky until reboot; watch disk)")
    if r.disk_free_gb < cfg["disk_floor_gb"]:
        why.append(f"disk free {r.disk_free_gb:.0f}GB: swap headroom shrinking")
    return ("warn" if why else "ok"), why


# ── reflexes ────────────────────────────────────────────────────────────────
def _procs() -> list[tuple[int, str]]:
    rows = []
    for line in _run(["ps", "-axo", "pid=,command="]).splitlines():
        line = line.strip()
        if " " in line and line.split(" ", 1)[0].isdigit():
            pid, cmd = line.split(" ", 1)
            rows.append((int(pid), cmd))
    return rows


def matching(patterns: list[str], procs: list[tuple[int, str]]) -> list[tuple[int, str]]:
    me = {os.getpid(), os.getppid()}
    rx = [re.compile(p) for p in patterns]
    return [(pid, cmd) for pid, cmd in procs
            if pid > 1 and pid not in me and "pressure_valve" not in cmd and any(r.search(cmd) for r in rx)]


def _term(pid: int, dry: bool) -> bool:
    if dry:
        return True
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _notify(title: str, msg: str) -> None:
    msg = msg.replace('"', "'")[:200]
    _run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'])


def _log(cfg: dict, row: dict) -> None:
    p = Path(cfg["log_file"]).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **row}) + "\n")


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULTS)
    if path:
        cfg.update(json.loads(Path(path).expanduser().read_text()))
    return cfg


def watch(cfg: dict, dry: bool, once: bool = False) -> None:
    last_state, last_shed = None, 0.0
    while True:
        r = read(cfg)
        state, why = assess(r, cfg)
        acts = []
        if cfg["immune"]:
            for pid, cmd in matching(cfg["immune"], _procs()):
                if _term(pid, dry):
                    acts.append({"act": "immune", "pid": pid, "command": cmd[:120], "dry": dry})
        if state == "critical" and cfg["shed"] and time.time() - last_shed > cfg["shed_cooldown_s"]:
            for pid, cmd in matching(cfg["shed"], _procs()):
                if _term(pid, dry):
                    acts.append({"act": "shed", "pid": pid, "command": cmd[:120], "dry": dry})
            last_shed = time.time()
        status = {"state": state, "why": why, "reading": asdict(r), "acts": acts, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        sp = Path(cfg["status_file"]).expanduser()
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(status, indent=1))
        if state != last_state or acts:
            _log(cfg, {"state": state, "why": why, "acts": acts, "pressure": r.pressure_pct, "swap_mb": r.swap_mb, "disk_gb": r.disk_free_gb})
            print(f"[valve] {state}: {'; '.join(why) or 'all clear'}" + (f" · {len(acts)} action(s)" if acts else ""), flush=True)
            if cfg["notify"] and last_state is not None and state != "ok":
                _notify(f"pressure-valve: {state}", "; ".join(why) or "")
        last_state = state
        if once:
            return
        time.sleep(cfg["interval_s"])


def _human(r: Reading, state: str, why: list[str]) -> str:
    lines = [f"state:            {state.upper()}",
             f"memory pressure:  {r.pressure_pct}%  (active+wired {r.committed_pct}%, free {r.free_pct}%)",
             f"kernel verdict:   {({1: 'normal', 2: 'warn', 4: 'critical'}).get(r.kernel_level, r.kernel_level)}",
             f"swap used:        {r.swap_mb:.0f} MB",
             f"disk free:        {r.disk_free_gb} GB"]
    lines += [f"  · {w}" for w in why]
    if r.leakers:
        lines.append("compressed-memory holders (leakers hide here, not in RSS):")
        lines += [f"  {pid:>7}  {v['cmprs_mb']:>6} MB  {v['command'][:90]}" for pid, v in r.leakers.items()]
    return "\n".join(lines)


def main() -> int:
    if sys.platform != "darwin":
        print("pressure-valve reads macOS-only counters (vm_stat, memory_pressure, sysctl).", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["status", "watch"])
    ap.add_argument("--config")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="log what would be killed, kill nothing")
    a = ap.parse_args()
    cfg = load_config(a.config)
    if a.cmd == "status":
        r = read(cfg)
        state, why = assess(r, cfg)
        print(json.dumps({"state": state, "why": why, **asdict(r)}, indent=1) if a.json else _human(r, state, why))
        return 0
    watch(cfg, a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
