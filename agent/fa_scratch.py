"""Throwaway headless Factorio instance for testing agent-bridge changes (never the live server).

    python fa_scratch.py setup     copy the live save + current mod source into runtime/scratch (write-data)
    python fa_scratch.py version   mod version on scratch vs live (must match before gating)
    python fa_scratch.py start | stop | mod (recopy mod; needs restart)
    python fa_scratch.py fresh         new map from the live map-gen settings (same seed) as the scratch save
    python fa_scratch.py raw "/sc ..."
    python fa_scratch.py call cmd key=val ...     raw bridge call (agent beltprobe)
    python fa_scratch.py t tool key=val ...       MCP tool with terse output (agent beltprobe)
"""
import json, os, shutil, subprocess, sys, time
from pathlib import Path


ROOT = Path(r"E:\modding\factorio-agent")
W = ROOT / "runtime" / "scratch"
FDIR = Path(r"D:\SteamLibrary\steamapps\common\Factorio")
EXE = FDIR / "bin" / "x64" / "factorio.exe"
RCON = {"host": "127.0.0.1", "port": 27115, "password": "scratchpw", "game_port": 34297}
sys.path.insert(0, str(ROOT / "agent"))
import server  # noqa
server.rcon_settings = lambda: RCON
_ping = server.ping
server.start = lambda wait=True: (_ for _ in ()).throw(SystemExit("scratch not running"))


def copy_mod():
    dst = W / "mods" / "agent-bridge"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(ROOT / "mod" / "agent-bridge", dst)


def setup():
    for d in ("config", "mods", "saves", "settings"):
        (W / d).mkdir(parents=True, exist_ok=True)
    (W / "config" / "config.ini").write_text(
        f"[path]\nread-data={FDIR / 'data'}\nwrite-data={W}\n[other]\ncheck-updates=false\n")
    copy_mod()
    shutil.copy(ROOT / "runtime" / "mods" / "mod-list.json", W / "mods" / "mod-list.json")
    s = json.loads((ROOT / "runtime" / "settings" / "server-settings.json").read_text())
    s["autosave_interval"] = 0
    s["name"] = "scratch"
    (W / "settings" / "server-settings.json").write_text(json.dumps(s))
    saves = sorted((ROOT / "runtime" / "saves").glob("*.zip"), key=lambda p: p.stat().st_mtime)
    shutil.copy(saves[-1], W / "saves" / "test.zip")
    print("copied", saves[-1])


def fresh():
    """A brand-new map with the live server's map-gen settings (its seed): the staged builder's test bed."""
    gen = ROOT / "runtime" / "settings" / "map-gen-settings.json"
    save = W / "saves" / "test.zip"
    if save.exists():
        save.unlink()
    r = subprocess.run([str(EXE), "--config", str(W / "config" / "config.ini"), "--mod-directory", str(W / "mods"),
                        "--create", str(save), "--map-gen-settings", str(gen)],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0 or not save.exists():
        print(r.stdout[-3000:], r.stderr[-1000:])
        raise SystemExit("map creation failed")
    print("created", save, "seed", json.loads(gen.read_text())["seed"])


def version():
    import rcon
    live = json.loads((ROOT / "runtime" / "rcon.json").read_text())
    q = '/silent-command rcon.print(script.active_mods["agent-bridge"])'
    a = rcon.Rcon(RCON["host"], RCON["port"], RCON["password"]).command(q).strip()
    b = rcon.Rcon(live["host"], live["port"], live["password"]).command(q).strip()
    print(f"scratch={a} live={b}", "OK" if a == b else "MISMATCH")


def start():
    args = [str(EXE), "--config", str(W / "config" / "config.ini"), "--mod-directory", str(W / "mods"),
            "--start-server", str(W / "saves" / "test.zip"),
            "--server-settings", str(W / "settings" / "server-settings.json"),
            "--rcon-bind", f"127.0.0.1:{RCON['port']}", "--rcon-password", RCON["password"],
            "--port", str(RCON["game_port"])]
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    out = open(W / "stdout.log", "w")
    p = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         creationflags=flags, cwd=str(W))
    (W / "pid").write_text(str(p.pid))
    for _ in range(180):
        if _ping():
            print("up pid", p.pid)
            return
        if p.poll() is not None:
            print((W / "stdout.log").read_text()[-3000:])
            raise SystemExit("exited")
        time.sleep(1)


def stop():
    from rcon import Rcon
    try:
        Rcon(RCON["host"], RCON["port"], RCON["password"]).command("/quit")
    except Exception as e:
        print("quit:", e)
    pid = int((W / "pid").read_text())
    for _ in range(60):
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        if "factorio" not in r.stdout.lower():
            print("stopped")
            return
        time.sleep(0.5)
    subprocess.run(["taskkill", "/PID", str(pid), "/F"])


def main():
    cmd = sys.argv[1]
    if cmd in ("setup", "start", "stop", "version", "fresh"):
        return globals()[cmd]()
    if cmd == "mod":
        return copy_mod()
    from bridge import Bridge
    b = Bridge("beltprobe", autostart=False)
    if cmd == "raw":
        print(b.raw(" ".join(sys.argv[2:])))
        return
    import cli
    if cmd == "call":
        cli.run(b, sys.argv[2:])
    elif cmd == "t":
        cli.run(b, ["t"] + sys.argv[2:])


if __name__ == "__main__":
    main()
