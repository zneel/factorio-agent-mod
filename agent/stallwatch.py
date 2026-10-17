"""Wake the architect when the crew is about to stall: exits (so a background run notifies its caller)
when no job is open or claimable for a character and that character idles, when an always-on worker
goes offline, or when a job blocks. Read-only; spawns no character.

    python stallwatch.py [--every 20] [--idle 90]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bridge import Bridge, BridgeError  # noqa: E402
import crew  # noqa: E402


def check(b, idle_limit, seen_blocked, check_offline=True):
    jobs = [j for j in crew._list_of(b.call("jobs", recent=0).get("jobs")) if isinstance(j, dict)]
    done_ids = {j["id"] for j in crew._list_of(b.call("jobs", all=True).get("jobs"))
                if isinstance(j, dict) and j.get("state") == "done"}
    members = crew._dict_of(crew._dict_of(b.call("team", chat=0)).get("members"))
    claimed = {j.get("by") for j in jobs if j.get("state") == "claimed"}
    reasons = []
    for name, m in sorted(members.items()):
        if name in claimed:
            continue
        # a job this character could claim now: open, for it or anyone, dependency done
        claimable = [j for j in jobs if j.get("state") == "open" and j.get("for") in (None, name)
                     and (not j.get("after") or j["after"] in done_ids)]
        if not claimable and (m or {}).get("idle_s", 0) > idle_limit:
            reasons.append(f"{name} idle {m['idle_s']}s with nothing claimable")
    off = crew.offline_workers(b) if check_offline else []
    if off:
        reasons.append("worker offline: " + ", ".join(off))
    for j in jobs:
        if j.get("state") == "blocked" and j["id"] not in seen_blocked:
            seen_blocked.add(j["id"])
            reasons.append(f"#{j['id']} blocked: {str(j.get('note'))[:160]}")
    return reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=20)
    ap.add_argument("--idle", type=int, default=90)
    a = ap.parse_args()
    b = Bridge("planner", autostart=False)
    seen = {j["id"] for j in crew._list_of(b.call("jobs", recent=0).get("jobs"))
            if isinstance(j, dict) and j.get("state") == "blocked"}  # only new blocks wake us
    t0 = time.time()
    while True:
        try:
            reasons = check(b, a.idle, seen, check_offline=time.time() - t0 > 300)  # workers need time to start
        except BridgeError as e:
            reasons = []
            print(f"server: {e}", flush=True)
        if reasons:
            print("STALL: " + " | ".join(reasons), flush=True)
            return
        time.sleep(a.every)


if __name__ == "__main__":
    main()
