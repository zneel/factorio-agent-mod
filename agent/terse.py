"""Token-lean rendering of bridge results.

JSON costs tokens on every brace, quote and repeated key. Agents read this form just as well:
    state=done t=194 pos=46,13 inv+=iron-plate:317,coal:-5
Rules: {x,y} -> "x,y"; {item: n} maps -> "a:1,b:2"; nested dicts in (); lists in [] joined by "; ";
noise keys (dist, kind, id of finished tasks, empty values) dropped.
"""

DROP = {"kind", "started_with", "bbox", "minable"}
RENAME = {"inventory_delta": "inv+", "position": "pos", "ticks": "t", "inventory": "inv",
          "free_slots": "free", "crafting_queue": "queue", "result": "res"}


def _num(v):
    if isinstance(v, float):
        r = round(v, 1)
        return str(int(r)) if r == int(r) else str(r)
    return str(v)


def _is_pos(v):
    return isinstance(v, dict) and set(v) == {"x", "y"}


def _is_counts(v):
    return isinstance(v, dict) and v and all(isinstance(x, (int, float)) for x in v.values()) \
        and not _is_pos(v)


def val(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (int, float)):
        return _num(v)
    if isinstance(v, str):
        return v
    if _is_pos(v):
        return f"{_num(v['x'])},{_num(v['y'])}"
    if _is_counts(v):
        return ",".join(f"{k}:{_num(n)}" for k, n in v.items())
    if isinstance(v, dict):
        inner = kv(v)
        return f"({inner})" if inner else ""
    if isinstance(v, list):
        if v and all(_is_pos(x) for x in v):
            return " ".join(val(x) for x in v)
        return "[" + "; ".join(val(x) for x in v) + "]"
    return str(v)


OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}


def kv(d: dict) -> str:
    if "inserter" in str(d.get("name", "")) and d.get("dir") in OPPOSITE:
        d = dict(d, dir=OPPOSITE[d["dir"]])  # engine names the pickup side; show where items go
    parts = []
    for k, v in d.items():
        if k in DROP or v is None or v == {} or v == [] or v == "":
            continue
        if k == "health" and isinstance(v, str) and v.partition("/")[0] == v.partition("/")[2]:
            continue  # full health is noise
        s = val(v)
        if s == "":
            continue
        parts.append(f"{RENAME.get(k, k)}={s}")
    return " ".join(parts)


def render(res) -> str:
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        return kv(res)
    return val(res)
