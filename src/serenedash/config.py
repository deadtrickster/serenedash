"""serenedash.config"""
import os
import subprocess
import tomllib



# ── configuration ───────────────────────────────────────────────────────────────────────────────
#
# Four layers, highest first: command line, environment, config file, defaults. The environment
# beats the file on purpose — that is what makes direnv work. A `.envrc` in a checkout exports
# SERENEDB_CONTAINER and friends, so `cd`ing into a project points the dashboard at that project's
# server without editing anything global, and leaving the directory points it back.
#
# The defaults below name no host, container, or password. This tool spent its first weeks with one
# deployment's container name compiled into it, which is fine until someone else runs it and every
# panel reports on a container they do not have.
#
# `--print-config` prints the resolved values AND where each came from, because a four-layer
# precedence you cannot inspect is worse than one hard-coded value: at least the hard-coded one is
# greppable.
DEFAULTS = {
    # How to reach the server. Everything else in this file used to assume "docker", in four
    # separate places, which made the tool unusable against a serened that simply runs on a host.
    #
    #   docker  container on this machine. psql runs inside it, du runs inside it, the pid comes
    #           from docker inspect. Filesystem and per-thread panels all work.
    #   local   a process on this machine. psql on the host, du on the host, pid from pgrep.
    #           Needs a psql client here; the container was supplying one before.
    #   remote  reachable only over the wire. The SQL panels work; anything that reads a filesystem
    #           or /proc does not, and says so rather than rendering an empty box - unless
    #           `proc_root` supplies one, which is what makes a server inside a VM readable.
    "target": "docker",
    "process": "serened",           # what to pgrep for when target = local
    "container": "serenedb",
    "host": "127.0.0.1",
    "port": "7890",
    "user": "postgres",
    "database": "postgres",
    "password": "",
    "data": "/var/lib/serenedb",
    # Where the SERVER's /proc is. "/proc" is this machine's, which is right whenever the server
    # shares this kernel - a container does, and its pids are visible here.
    #
    # A VM does not. Its /proc belongs to another kernel, and reading this one instead does not
    # fail: pid 1400 exists here too, belonging to something else entirely, so the panels fill with
    # confident numbers about the wrong process. Point this at a mount of the guest's /proc and
    # they describe the server again.
    #
    # Only the reads ABOUT THE SERVER follow this. Whether kernel symbols resolve, and which agent
    # is calling the MCP server, are questions about the machine doing the looking, and those keep
    # reading /proc directly.
    "proc_root": "/proc",
    "perf_dir": os.path.join(os.environ.get("XDG_CACHE_HOME",
                                            os.path.expanduser("~/.cache")), "serenedash", "perf"),
    "interval": 5.0,
    # Pointer tracking: hover tooltips, click to open a panel, wheel to scroll. On by default
    # because the tooltips are the answer to "what is this number", but it is a real trade — while
    # tracking is on the terminal's own text selection is off (Shift usually bypasses it), and some
    # people simply do not want a box appearing under the pointer. `x` toggles it for a moment;
    # this turns it off for good.
    "mouse": True,
    # Where to look for a binary matching a capture's build-id. Symbols resolve for an unprivileged
    # reader only from a registered build, and the build that produced a capture is often sitting in
    # somebody's build tree. See `symbol_sources`.
    "symbol_paths": [],
    # Shell command printing the password on stdout, e.g. "pass show serenedb". Preferred over
    # `password`: a config file is a file on disk, and this keeps the secret in whatever already
    # holds your secrets.
    "password_command": "",
}


ENV = {
    "container": "SERENEDB_CONTAINER", "port": "SERENEDB_PORT", "password": "PGPASSWORD",
    "data": "SERENEDB_DATA", "perf_dir": "SERENEDASH_PERF_DIR", "interval": "SERENEDASH_INTERVAL",
    "symbol_paths": "SERENEDASH_SYMBOL_PATHS", "password_command": "SERENEDASH_PASSWORD_COMMAND",
    "target": "SERENEDB_TARGET", "host": "SERENEDB_HOST", "process": "SERENEDB_PROCESS",
    "proc_root": "SERENEDASH_PROC_ROOT",
    "user": "SERENEDB_USER", "database": "SERENEDB_DATABASE", "mouse": "SERENEDASH_MOUSE",
}


# Values that are a yes/no rather than a string. The environment and the command line both deliver
# them as text, and `bool("0")` is True — a variable set to 0 to turn something off turning it on
# is the sort of thing nobody tests and everybody hits.
FLAGS = ("mouse",)


def as_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def config_files():
    """Config files in increasing precedence: global, then per-project.

    The project file is looked up from the working directory so a checkout can carry the settings
    for the server it belongs to, the same way `.envrc` does — for people who would rather commit a
    file than depend on direnv.
    """
    home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    out = [os.path.join(home, "serenedash", "config.toml")]
    if os.environ.get("SERENEDASH_CONFIG"):
        out.append(os.environ["SERENEDASH_CONFIG"])
    out.append(os.path.join(os.getcwd(), "serenedash.toml"))
    return out


def load_config(cli=None):
    """Resolve the four layers. Returns (values, provenance) — provenance names the winning layer."""
    vals = dict(DEFAULTS)
    prov = dict.fromkeys(vals, "default")
    for path in config_files():
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for k, v in data.items():
            if k in vals:
                vals[k], prov[k] = v, path
    for k, var in ENV.items():
        if os.environ.get(var):
            raw = os.environ[var]
            vals[k] = raw.split(os.pathsep) if k == "symbol_paths" else raw
            prov[k] = f"${var}"
    for k, v in (cli or {}).items():
        if v is not None and k in vals:
            vals[k], prov[k] = v, "flag"
    vals["interval"] = float(vals["interval"])
    for k in FLAGS:
        vals[k] = as_bool(vals[k])
    # Resolved last so the command runs only once, and only if nothing more direct supplied one.
    if not vals["password"] and vals["password_command"]:
        try:
            out = subprocess.run(vals["password_command"], shell=True, capture_output=True,
                                 text=True, timeout=30)
            vals["password"], prov["password"] = out.stdout.strip(), "password_command"
        except Exception:                                       # noqa: BLE001
            pass
    return vals, prov
