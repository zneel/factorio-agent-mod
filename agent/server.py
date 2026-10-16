"""Create, start and stop the headless Factorio server the agent plays on.

    python server.py create [--seed N] [--enemies] [--force]   new map (runtime/saves/agent.zip)
    python server.py start                                      start headless server (creates map if missing)
    python server.py stop
    python server.py status

The server uses its own write-data directory (factorio-agent/runtime), so it never touches the
normal game profile in %APPDATA%\\Factorio. Humans can watch by joining localhost:34197 from the
regular game (Multiplayer > Connect to address).
"""

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUNTIME = ROOT / "runtime"
MOD_SRC = ROOT / "mod" / "agent-bridge"
FACTORIO_DIR = Path(os.environ.get("FACTORIO_DIR", r"D:\SteamLibrary\steamapps\common\Factorio"))
FACTORIO_EXE = FACTORIO_DIR / "bin" / "x64" / "factorio.exe"
SAVE = RUNTIME / "saves" / "agent.zip"
CONFIG = RUNTIME / "config" / "config.ini"
RCON_FILE = RUNTIME / "rcon.json"
PID_FILE = RUNTIME / "server.pid"
LOG_FILE = RUNTIME / "server-stdout.log"

sys.path.insert(0, str(HERE))
from rcon import Rcon, RconError  # noqa: E402


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def rcon_settings() -> dict:
    if RCON_FILE.exists():
        return json.loads(RCON_FILE.read_text())
    s = {"host": "127.0.0.1", "port": 27015, "password": secrets.token_hex(12), "game_port": 34197}
    RUNTIME.mkdir(parents=True, exist_ok=True)
    RCON_FILE.write_text(json.dumps(s, indent=2))
    return s


def prepare_runtime():
    for d in ("config", "mods", "saves", "settings"):
        (RUNTIME / d).mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(
        "[path]\n"
        f"read-data={FACTORIO_DIR / 'data'}\n"
        f"write-data={RUNTIME}\n"
        "[other]\n"
        "check-updates=false\n"
    )
    # install the bridge mod (copied fresh each start so edits apply on restart)
    dst = RUNTIME / "mods" / "agent-bridge"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(MOD_SRC, dst)
    mods = [{"name": "base", "enabled": True}, {"name": "agent-bridge", "enabled": True}]
    for dlc in ("elevated-rails", "quality", "space-age"):
        if (FACTORIO_DIR / "data" / dlc).exists():
            mods.append({"name": dlc, "enabled": False})
    (RUNTIME / "mods" / "mod-list.json").write_text(json.dumps({"mods": mods}, indent=2))

    settings = {
        "name": "factorio-agent",
        "description": "AI agent playground",
        "visibility": {"public": False, "lan": False},
        "require_user_verification": False,
        "max_players": 0,
        "allow_commands": "admins-only",
        "autosave_interval": 5,
        "autosave_slots": 5,
        "afk_autokick_interval": 0,
        "auto_pause": False,
        "auto_pause_when_players_connect": False,
        "only_admins_can_pause_the_game": True,
        "autosave_only_on_server": True,
    }
    (RUNTIME / "settings" / "server-settings.json").write_text(json.dumps(settings, indent=2))


def factorio(*args) -> list:
    return [str(FACTORIO_EXE), "--config", str(CONFIG), "--mod-directory", str(RUNTIME / "mods"), *args]


def create(seed=None, enemies=False, force=False):
    prepare_runtime()
    if SAVE.exists() and not force:
        log(f"{SAVE} already exists (use --force to replace it)")
        return
    gen = json.loads((FACTORIO_DIR / "data" / "map-gen-settings.example.json").read_text())
    gen = {k: v for k, v in gen.items() if not k.startswith("_")}
    gen["seed"] = seed
    if not enemies:
        gen["peaceful_mode"] = True
        gen["autoplace_controls"]["enemy-base"] = {"frequency": 0, "size": 0}
    gen_path = RUNTIME / "settings" / "map-gen-settings.json"
    gen_path.write_text(json.dumps(gen, indent=2))
    if SAVE.exists():
        SAVE.unlink()
    log("creating map ...")
    r = subprocess.run(factorio("--create", str(SAVE), "--map-gen-settings", str(gen_path)),
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not SAVE.exists():
        log(r.stdout[-4000:], r.stderr[-2000:])
        raise SystemExit("map creation failed")
    log(f"created {SAVE}")


def ping(timeout=2.0) -> bool:
    s = rcon_settings()
    try:
        c = Rcon(s["host"], s["port"], s["password"], timeout=timeout)
        c.connect()
        c.close()
        return True
    except (OSError, RconError):
        return False


def running_pid():
    if not PID_FILE.exists():
        return None
    pid = int(PID_FILE.read_text().strip() or 0)
    r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
    return pid if "factorio" in r.stdout.lower() else None


def start(wait=True):
    if ping():
        log("server already running")
        return
    if running_pid():
        log("server process exists but RCON is not answering yet; waiting")
    else:
        prepare_runtime()
        if not SAVE.exists():
            create()
        s = rcon_settings()
        args = factorio(
            "--start-server", str(SAVE),
            "--server-settings", str(RUNTIME / "settings" / "server-settings.json"),
            "--rcon-bind", f"{s['host']}:{s['port']}", "--rcon-password", s["password"],
            "--port", str(s["game_port"]),
        )
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        out = open(LOG_FILE, "w", encoding="utf-8", errors="replace")
        p = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True, cwd=str(RUNTIME))
        PID_FILE.write_text(str(p.pid))
        log(f"started factorio pid {p.pid}; log: {RUNTIME / 'factorio-current.log'}")
    if wait:
        deadline = time.time() + 180
        while time.time() < deadline:
            if ping():
                log("server ready (RCON up)")
                return
            if not running_pid():
                tail = (RUNTIME / "factorio-current.log").read_text(errors="replace")[-3000:] \
                    if (RUNTIME / "factorio-current.log").exists() else ""
                raise SystemExit("factorio exited during startup:\n" + tail)
            time.sleep(1)
        raise SystemExit("timed out waiting for RCON")


def stop():
    s = rcon_settings()
    if ping():
        c = Rcon(s["host"], s["port"], s["password"])
        try:
            c.command("/quit")
        except (OSError, RconError):
            pass
    pid = running_pid()
    deadline = time.time() + 60
    while pid and running_pid() and time.time() < deadline:
        time.sleep(0.5)
    if running_pid():
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    if PID_FILE.exists():
        PID_FILE.unlink()
    log("server stopped")


def status():
    s = rcon_settings()
    print(json.dumps({"pid": running_pid(), "rcon_up": ping(), "rcon": f"{s['host']}:{s['port']}",
                      "join_address": f"localhost:{s['game_port']}", "save": str(SAVE)}, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--seed", type=int)
    c.add_argument("--enemies", action="store_true", help="generate biter bases (default: peaceful, none)")
    c.add_argument("--force", action="store_true", help="replace the existing map")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "create":
        create(a.seed, a.enemies, a.force)
    elif a.cmd == "start":
        start()
    elif a.cmd == "stop":
        stop()
    else:
        status()


if __name__ == "__main__":
    main()
