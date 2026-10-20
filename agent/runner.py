"""Queue runner: builds gated plans with one thread per character, no agents in the loop.

    .venv/Scripts/python runner.py            run (Ctrl+C / kill to stop); log in runtime/runner.log
    .venv/Scripts/python runner.py status     what each character does, queue state

runtime/queue.json (re-read every cycle, so the overseer can append while it runs):
    {"builders": ["w1", "w2", "w3"], "tender": "w4",
     "tend": ["runtime/blocks/new/boot-copper.json", ...],          # burners the tender keeps fuelled
     "items": [{"name": "oil1", "plan": "runtime/blocks/new/oil1.json", "after": ["sci1fix"], "for": "w1"?}]}
A builder takes the first item whose `after` items are done and that nobody else is building, and runs
supply.build_plan on it. Done items are recorded in runtime/queue-state.json. An unfinished build (shortage, or a
machine whose research is not done yet) goes back to the queue and is retried after RETRY seconds.
"""
import json
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
from bridge import Bridge  # noqa: E402
import bootstrap  # noqa: E402
import supply  # noqa: E402

QUEUE = ROOT / "runtime" / "queue.json"
STATE = ROOT / "runtime" / "queue-state.json"
LOG = ROOT / "runtime" / "runner.log"
RETRY = 90
lock = threading.Lock()


def log(*a):
    line = time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a)
    with lock:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def load(p, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def state():
    return load(STATE, {"done": [], "retry_at": {}, "running": {}, "last": {}})


def save_state(st):
    STATE.write_text(json.dumps(st, indent=1))


def pick(char):
    with lock:
        q, st = load(QUEUE, {}), state()
        for it in q.get("items", []):
            n = it["name"]
            if n in st["done"] or n in st["running"].values():
                continue
            if it.get("for") and it["for"] != char:
                continue
            if any(d not in st["done"] for d in it.get("after", [])):
                continue
            if st["retry_at"].get(n, 0) > time.time():
                continue
            st["running"][char] = n
            save_state(st)
            return it
    return None


def finish(char, it, ok, note):
    with lock:
        st = state()
        st["running"].pop(char, None)
        st["last"][it["name"]] = note[:200]
        if ok:
            if it["name"] not in st["done"]:
                st["done"].append(it["name"])
            st["retry_at"].pop(it["name"], None)
        else:
            st["retry_at"][it["name"]] = time.time() + RETRY
        save_state(st)


def builder(char):
    b = Bridge(char)
    while True:
        it = pick(char)
        if not it:
            time.sleep(10)
            continue
        log(char, "start", it["name"])
        try:
            r = supply.build_plan(b, it["plan"], it.get("minutes", 4))
            ok = bool(r.get("done"))
            res = r.get("result") or {}
            note = "DONE" if ok else f"partial: placed {res.get('placed', 0)} short {r.get('short')} failed {res.get('failed')}"
        except Exception as e:  # keep the thread alive: log and retry later
            ok, note = False, f"error {e!r}"
            log(char, traceback.format_exc()[-600:])
        log(char, it["name"], note)
        finish(char, it, ok, note)
        if ok:
            try:
                supply.deposit(b)
            except Exception:
                pass


def tender(char):
    b = Bridge(char)
    while True:
        q = load(QUEUE, {})
        plans = [e for p in q.get("tend", []) for e in bootstrap.load(p)]
        try:
            if plans:
                bootstrap.ensure_coal(b, 40)
                bootstrap.tend(b, plans)
                supply.deposit(b, everything=False)
            else:
                time.sleep(20)
        except Exception as e:
            log(char, "tend error", repr(e)[:200])
            time.sleep(10)


def status():
    q, st = load(QUEUE, {}), state()
    print("running:", st["running"])
    for it in q.get("items", []):
        n = it["name"]
        s = "done" if n in st["done"] else ("running" if n in st["running"].values() else
                                            f"retry in {int(st['retry_at'].get(n, 0) - time.time())}s" if st["retry_at"].get(n, 0) > time.time() else "waiting")
        print(f"  {n:14} {s:14} {st['last'].get(n, '')[:110]}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "status":
        return status()
    q = load(QUEUE, {})
    st = state()
    st["running"] = {}
    save_state(st)
    threads = [threading.Thread(target=builder, args=(c,), daemon=True) for c in q.get("builders", [])]
    if q.get("tender"):
        threads.append(threading.Thread(target=tender, args=(q["tender"],), daemon=True))
    for t in threads:
        t.start()
    log("runner up:", q.get("builders"), "tender", q.get("tender"))
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
