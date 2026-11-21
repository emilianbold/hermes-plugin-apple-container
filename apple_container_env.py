"""Apple container execution environment: a long-lived Linux VM per sandbox.

One container per (Hermes home, task), created with ``container run -d --init
--entrypoint env ... sleep infinity`` and reused across tool calls, Hermes
processes and sessions (`persistent`; stopped while idle) or deleted when its
session ends.
Commands run via ``container exec ... bash -c``. File tools go through the
same ``exec`` channel (``uses_host_filesystem`` stays False), so they see
exactly what the terminal sees — nothing on the Mac unless the user shared a
folder.

What a volume spec or an extra argument means is decided in ``specs.py``, the
same parser the provider's approval decision uses. The backend keeps a little
state of its own under ``<HERMES_HOME>/plugin-data/apple_container/``:
``leases/`` (which living process uses a container), ``locks/`` (one lock
file per sandbox, held while it is stopped or attached to) and ``staged/``
(private copies of the credential files the user chose to share).

A persistent sandbox that no living environment uses is STOPPED, never
deleted (``apple_container_keep_running`` opts out): the container and its
volumes stay and the next command starts it again.

Fail-closed everywhere: a missing CLI, a stopped service, a refused setting
or a failed start raises ``EnvironmentConnectionError``; there is never a
fallback to the host.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import logging
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from tools.environments.base import BaseEnvironment, EnvironmentConnectionError

from .provider import cli_env, find_container_executable, hermes_mount_kinds, unsupported_host_reason
from .settings import ENV_NAME_RE as _ENV_NAME_RE
from .specs import ExtraArgs, Mount, SpecError, parse_extra_args, parse_volumes, same_image

logger = logging.getLogger(__name__)

DEFAULT_SHM_SIZE = "1g"

# Guest-side timeout backstop: the host kills the exec client at the tool
# deadline; the in-guest `timeout` then stops the command's whole process
# group shortly AFTER (the tool result has already been returned by then).
_GUEST_TIMEOUT_GRACE_S = 3
_GUEST_TIMEOUT_KILL_S = 5

# Every exec's wrapped script carries this token in its argv; a kill runs
# `pkill -f` against a bracketed form of it (the bracket keeps the killer's
# own command line from matching the pattern).
_EXEC_TOKEN_PREFIX = "hermes-exec-"

# Apple container label values may not contain spaces or commas.
_LABEL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")

# A stopped, *non-persistent* shared container younger than this may belong to
# a sibling process about to reuse it, so the sweep leaves it alone.
_SWEEP_STALE_AFTER_S = 900.0

# How long an environment that attaches to a sandbox waits for its lock (see
# _sandbox_lock) before going ahead without it: a stop in progress is over
# long before. A stop never waits for the lock.
_ATTACH_LOCK_WAIT_S = 90.0

# Once-per-process startup sweep guard (per pid; tests re-arm it).
_SWEEP_DONE: set[int] = set()
_SWEEP_LOCK = threading.Lock()

# (exe, image) -> whether the guest has a `timeout` binary. Probed lazily
# through one exec; a failed probe reads as "no" (bare bash + pkill fallback).
_GUEST_TIMEOUT_CACHE: dict[tuple[str, str], bool] = {}


def _sanitize_label(value: str) -> str:
    """Label-safe token: Apple container rejects spaces and commas."""
    return _LABEL_UNSAFE_RE.sub("_", str(value))[:63] or "unknown"


def _home_key() -> str:
    """Short stable key of the active Hermes home: part of every container
    and volume name, and the ``hermes-home`` label the sweep is scoped by."""
    from hermes_constants import get_hermes_home
    return hashlib.sha256(str(get_hermes_home()).encode("utf-8")).hexdigest()[:8]


def container_identity(task_id: str, *, persistent: bool = True,
                       workspace: str = "") -> str:
    """Deterministic name suffix for a sandbox.

    Keyed on the Hermes home (profiles and throwaway homes never share a
    sandbox). In persistent mode ONE sandbox per home is shared by every
    session — core hands plugin backends per-session task ids even when
    ``container_persistent`` is true, so keying the name on task_id there
    would fragment the sandbox one container per chat; this mirrors the Docker
    backend's profile-scoped persistent semantics. ``workspace`` (the
    directory mounted with ``apple_container_mount_cwd``) gets its own sandbox,
    like Docker's per-workspace mounting. Non-persistent mode keys the task id
    so each session gets its own identity."""
    home_key = _home_key()
    if persistent:
        parts = [home_key]
        if workspace:
            parts.append("w" + hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:8])
        return "-".join(parts)
    task_key = hashlib.sha256(str(task_id or "default").encode("utf-8")).hexdigest()[:10]
    return f"{home_key}-{task_key}"


def _format_cpus(cpu: Any) -> Optional[str]:
    """Apple's CLI takes an INTEGER vCPU count (Docker accepts fractions like
    0.5); fractions are rounded half-up to at least one vCPU, with a warning."""
    try:
        value = float(cpu)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    nearest = max(1, int(value + 0.5))
    if nearest != value:
        logger.warning("apple_container: --cpus needs an integer; rounding %s to %d vCPU",
                       cpu, nearest)
    return str(nearest)


def run_capture(argv: list[str], *, timeout: float, env: dict | None = None,
                check: bool = False) -> subprocess.CompletedProcess:
    """``subprocess.run`` with text capture and stdin closed (a CLI that
    prompts must never hang the agent). The program gets the short
    environment of ``cli_env()`` unless *env* is given, never Hermes' own."""
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, check=check,
                          stdin=subprocess.DEVNULL, env=cli_env() if env is None else env)


def bash_argv(cmd_string: str, login: bool = False) -> list[str]:
    return ["bash", "-l", "-c", cmd_string] if login else ["bash", "-c", cmd_string]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True  # unknown: be conservative


def _host_timezone() -> Optional[str]:
    """The Mac's zone as a tz-database name, or None. Forwarded as TZ so the
    sandbox's timestamps match the user's clock (the guest defaults to UTC)."""
    tz = os.environ.get("TZ")
    if tz:
        return tz
    try:  # macOS/Linux: /etc/localtime is a symlink into the zoneinfo tree
        link = os.readlink("/etc/localtime")
        tail = link.split("zoneinfo/")[-1]
        if tail and "/" in link and "zoneinfo" in link:
            return tail
    except OSError:
        pass
    try:
        info = datetime.datetime.now().astimezone().tzinfo
        return getattr(info, "key", None)  # zoneinfo.ZoneInfo on some builds
    except Exception:
        return None


def _feed_stdin(proc: subprocess.Popen, data: str) -> None:
    """Write *data* to the process's stdin from a thread, then close it. A
    thread, because a file larger than the pipe buffer would otherwise block
    against output nobody is reading yet. The bytes are UTF-8 we encode
    ourselves (``surrogateescape``, the inverse of how output is decoded).
    Stdin is closed whatever happens, so the command sees end-of-input; a
    failed write is left on the process for Hermes to report as
    ``stdin_error``, and the thread for it to join before it reads that."""
    errors: list[BaseException] = []

    def write() -> None:
        target = getattr(proc.stdin, "buffer", proc.stdin)
        try:
            raw = data.encode("utf-8", "surrogateescape")
            if target.write(raw) != len(raw):
                raise RuntimeError("short write to the command's stdin")
        except OSError:
            pass  # the command exited or closed stdin without reading it all
        except Exception as exc:  # text with no UTF-8 form
            errors.append(exc)
        finally:
            try:
                target.close()
            except Exception:
                pass

    thread = threading.Thread(target=write, daemon=True)
    proc._hermes_stdin_errors = errors  # type: ignore[attr-defined]
    proc._hermes_stdin_thread = thread  # type: ignore[attr-defined]
    thread.start()


def _start_exec(argv: list[str], stdin_data: Optional[str],
                env: dict[str, str]) -> subprocess.Popen:
    """Start one `container exec` with its output on a single pipe and, when
    there is input, stdin fed by ``_feed_stdin``; no stdin at all otherwise."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.PIPE if stdin_data is not None
                            else subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", env=env)
    if stdin_data is not None:
        _feed_stdin(proc, stdin_data)
    return proc


def _client_env(values: dict[str, str]) -> dict[str, str]:
    """The CLI subprocess's environment: the short list of ``cli_env()`` plus
    the forwarded values (paired with name-only ``-e KEY`` flags so values
    never appear in the world-readable command line)."""
    return cli_env(values)


class AppleContainerConfigError(EnvironmentConnectionError):
    """A setting that is refused before anything is started (a share that
    would be wider than written, an argument the backend cannot honour).
    Raised instead of guessing: nothing runs, least of all on the Mac."""

    def __init__(self, reason: str):
        super().__init__(
            f"Apple Container backend: {reason}. No sandbox was started; commands never "
            "fall back to running on this Mac.",
            retry_hint="This is a configuration error, not a command failure: fix the "
                       "setting the message names (in config.yaml, or in Hermes Desktop "
                       "under Settings > Plugins), then retry.")


def _hermes_file_shares(kinds: tuple[str, ...]) -> tuple[list[Mount], list[tuple[str, str]]]:
    """What the user chose to share of Hermes' own files (``credentials``,
    ``skills``, ``cache``; none by default): read-only folder mounts for
    skills and cache, and ``(host file, container path)`` pairs for the
    registered credential files, which are staged, never mounted from where
    they are. Missing or wrong-kind sources are skipped."""
    mounts: list[Mount] = []
    credentials: list[tuple[str, str]] = []
    if not kinds:
        return mounts, credentials
    try:
        import tools.credential_files as cf
        getters = {"credentials": (cf.get_credential_file_mounts, True),
                   "skills": (cf.get_skills_directory_mount, False),
                   "cache": (cf.get_cache_directory_mounts, False)}
        for kind in kinds:
            getter, expects_file = getters[kind]
            for entry in getter():
                src = Path(entry["host_path"])
                if expects_file:
                    problem = ("source is a directory" if src.is_dir()
                               else None if src.is_file() else "source not found")
                else:
                    problem = None if src.is_dir() else "source is not a directory"
                if problem:
                    logger.info("apple_container: skipping hermes mount — %s: %s", problem, src)
                elif expects_file:
                    credentials.append((str(entry["host_path"]), str(entry["container_path"])))
                else:
                    mounts.append(Mount("host", str(entry["host_path"]),
                                        str(entry["container_path"]), readonly=True))
    except Exception as exc:
        logger.debug("apple_container: could not load hermes skill/credential mounts: %s", exc)
    return mounts, credentials


def _private_dir(path: Path) -> None:
    """A directory only the Mac user can enter."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def _copy_in_place(source: str, target: Path) -> None:
    """Make *target* a private copy of *source*, rewriting an existing copy
    IN PLACE: a running sandbox keeps seeing a file that is rewritten, and
    loses one that is replaced by rename (measured on container 1.2.2)."""
    data = Path(source).read_bytes()
    try:
        if target.read_bytes() == data:
            os.chmod(target, 0o600)
            return
    except OSError:
        pass
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(target, 0o600)


def _remove_staging(name: str, root: Optional[Path] = None) -> None:
    """Delete the staged files of container *name* (a folder of the plugin's
    own; nothing outside ``plugin-data/apple_container/staged`` is touched)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name or ""):
        return
    shutil.rmtree((root or _state_dir()) / "staged" / name, ignore_errors=True)


def _mount_key(kind: str, source: str) -> str:
    """Comparable identity of a mount source: a volume's name, or a host path
    the way ``container inspect`` reports it (normalised, symlinks kept)."""
    return f"volume:{source}" if kind == "volume" else os.path.normpath(source or "/")


# Top-level folders of a Mac; a working directory under one of them is a host
# path, not a place in the Linux sandbox.
_MAC_PATH_PREFIXES = ("/Users/", "/Volumes/", "/private/", "/var/folders/", "/Applications/",
                      "/Library/", "/System/", "/opt/homebrew/")

# Where credentials usually live, relative to the Mac home.
_CREDENTIAL_PLACES = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".netrc",
                      ".config/gcloud", ".config/gh", ".git-credentials", "Library/Keychains")


def _credential_places_in(source: str) -> list[str]:
    """Credential places a shared Mac folder exposes: the ones it is, lies
    inside of, or (when they exist) contains. The Hermes home counts: its
    ``.env`` and ``auth.json`` hold the model-provider keys."""
    def real(path: Any) -> str:
        return os.path.realpath(os.path.expanduser(str(path)))

    shared = real(source)
    places = [real(Path.home() / rel) for rel in _CREDENTIAL_PLACES]
    try:
        from hermes_constants import get_hermes_home
        places.append(real(get_hermes_home()))
    except Exception:
        pass
    hits = []
    for place in places:
        inside = shared == place or shared.startswith(place + "/")
        contains = place.startswith(shared.rstrip("/") + "/") and os.path.lexists(place)
        if inside or contains:
            hits.append(place)
    return hits


def _forward_env_names(value: Any) -> list[str]:
    """Validated ``apple_container_forward_env``: a list of variable names,
    trimmed, each once. Anything else is ignored with a warning — a string is
    NOT a list of one-letter names — so less is forwarded, never more."""
    if value is None:
        return []
    if not isinstance(value, list):
        logger.warning("apple_container_forward_env must be a list of variable names; "
                       "ignoring %r", value)
        return []
    names: list[str] = []
    for entry in value:
        name = entry.strip() if isinstance(entry, str) else ""
        if not _ENV_NAME_RE.match(name):
            logger.warning("Ignoring invalid apple_container_forward_env entry: %r", entry)
        elif name not in names:
            names.append(name)
    return names


def _literal_env(value: Any) -> dict[str, str]:
    """Validated ``apple_container_env``: ``NAME: value`` pairs. A key that is
    not a variable name is dropped with a warning; the others still arrive."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        logger.warning("apple_container_env must be a mapping of NAME: value; ignoring %r",
                       type(value).__name__)
        return {}
    pairs: dict[str, str] = {}
    for key, item in value.items():
        name = key.strip() if isinstance(key, str) else ""
        if not _ENV_NAME_RE.match(name):
            logger.warning("Ignoring invalid apple_container_env key: %r", key)
            continue
        pairs[name] = "" if item is None else str(item)
    return pairs


def _forwarded_value(name: str) -> Optional[str]:
    """The value of one variable the user put on the forward list, asked of
    Hermes itself: ``get_secret`` answers from the active profile's ``.env``,
    then the process environment, and under several profiles never with
    another profile's value. The plugin does not read the ``.env`` file.
    A lookup Hermes refuses skips the variable; the sandbox still starts."""
    try:
        from agent import secret_scope
        return secret_scope.get_secret(name)
    except Exception as exc:
        logger.warning("apple_container: forward-env %s could not be looked up (%s); "
                       "it is not forwarded", name, exc)
        return None


def _state_dir() -> Path:
    """The plugin's own folder under the Hermes home (leases, locks, staged
    files). One process can serve several homes, so an environment remembers
    the folder it was made under (``_state_root``) and passes it as *root* to
    the helpers below; without *root* they use the home in effect now."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "plugin-data" / "apple_container"


def _lease_dir(name: str, root: Optional[Path] = None) -> Path:
    return (root or _state_dir()) / "leases" / name


def _live_leases(name: str, root: Optional[Path] = None) -> list[Path]:
    """Leases on container *name* held by living processes. A lease is a file
    ``<pid>-<token>`` written by each environment that uses the container; one
    whose process is gone is removed here."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name or ""):
        return []
    try:
        entries = list(_lease_dir(name, root).iterdir())
    except OSError:
        return []
    live = []
    for entry in entries:
        pid = entry.name.split("-", 1)[0]
        if pid.isdigit() and _pid_alive(int(pid)):
            live.append(entry)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    return live


@contextlib.contextmanager
def _sandbox_lock(name: str, wait_s: float, root: Optional[Path] = None):
    """Exclusive lock on sandbox *name* across Hermes processes and threads;
    yields whether it was obtained within *wait_s* seconds (0: one try).

    Held while an unused sandbox is stopped and while an environment attaches
    to (or starts) one, so "nobody uses it" cannot stop being true between the
    check and the stop: an environment that arrives during a stop waits for
    it and then starts the sandbox, instead of taking over one that is about
    to go down. The lock files live beside the leases and are never removed;
    the folder above them is not created here (it exists once a lease was
    written)."""
    handle, locked = None, False
    try:
        folder = (root or _state_dir()) / "locks"
        folder.mkdir(exist_ok=True)
        handle = os.open(folder / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + wait_s
        while not locked:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
    except OSError:
        logger.debug("apple_container: no lock for %s", name, exc_info=True)
    try:
        yield locked
    finally:
        if handle is not None:
            os.close(handle)  # closing releases the lock


def _inspect_container(exe: str, name: str) -> Optional[dict]:
    """``container inspect NAME`` as a mapping; ``None`` when the container
    is missing (exit 1, empty stdout) or the answer cannot be read."""
    result = run_capture([exe, "inspect", name], timeout=15)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
        return data[0] if isinstance(data, list) and data else None
    except ValueError:
        return None


def _is_ours(labels: Any) -> bool:
    """Whether a container's labels say this backend made it."""
    return (isinstance(labels, dict) and labels.get("hermes-agent") == "1"
            and labels.get("hermes-backend") == "apple_container")


def _stop_if_unused(exe: str, name: str, root: Optional[Path] = None) -> bool:
    """Stop persistent sandbox *name* unless a living process uses it; True
    when it was stopped here.

    A stop, never a delete: the container and its volumes stay, so files and
    installed packages are there when the next command starts it again; only
    the processes running inside are lost. Ours only (labels re-checked). If
    it will not stop politely it is killed, which also leaves it in place.

    The sandbox's lock is tried once, not waited for: whoever holds it is
    attaching to the sandbox (it is in use) or already stopping it."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name or ""):
        return False
    with _sandbox_lock(name, 0, root) as locked:
        if not locked:
            logger.info("apple_container: leaving %s as it is: it is being attached to "
                        "or stopped", name)
            return False
        users = _live_leases(name, root)
        if users:
            logger.info("apple_container: leaving %s running: %d environment(s) use it",
                        name, len(users))
            return False
        try:
            existing = _inspect_container(exe, name)
        except (OSError, subprocess.SubprocessError):
            return False
        if existing is None or (existing.get("status") or {}).get("state") != "running":
            return False
        if not _is_ours((existing.get("configuration") or {}).get("labels")):
            logger.error("apple_container: refusing to stop %s — its labels are not ours", name)
            return False
        if _best_effort(run_capture, [exe, "stop", name], timeout=60):
            return True
        return _best_effort(run_capture, [exe, "kill", name], timeout=30)


def _remove_container(exe: str, name: str, *, running: bool) -> bool:
    """Stop and delete container *name* (the caller has checked it is ours).

    If that does not remove it — ``stop`` failed or timed out, and the CLI
    refuses a plain ``delete`` of a running container — kill it and delete
    with ``--force``, so a VM is never left running because it would not stop
    politely."""
    if running:
        _best_effort(run_capture, [exe, "stop", name], timeout=60)
    if _best_effort(run_capture, [exe, "delete", name], timeout=60):
        return True
    _best_effort(run_capture, [exe, "kill", name], timeout=30)
    return _best_effort(run_capture, [exe, "delete", "--force", name], timeout=60)


def _owner_pid(labels: dict) -> Optional[int]:
    """The pid in a ``hermes-owner=pid<N>`` label; ``None`` when unreadable."""
    owner = str(labels.get("hermes-owner", ""))
    return int(owner[3:]) if owner.startswith("pid") and owner[3:].isdigit() else None


def _sweep_reason(item: dict, home: str, profile: str) -> Optional[str]:
    """Why a listed container is a leftover to remove, or ``None`` to keep it."""
    cfg = item.get("configuration") or {}
    labels = cfg.get("labels") or {}
    if labels.get("hermes-agent") != "1" or labels.get("hermes-backend") != "apple_container":
        return None
    # Stay inside this Hermes home/profile: another home's sweep handles its own.
    if labels.get("hermes-home", home) != home or labels.get("hermes-profile", profile) != profile:
        return None
    name = item.get("id")
    if not name or _live_leases(name):
        return None  # a living process still uses it
    if labels.get("hermes-scope") == "session":
        pid = _owner_pid(labels)
        if pid is None or _pid_alive(pid):
            return None  # unknown is not dead
        return f"session container of dead process pid{pid}"
    if (labels.get("hermes-scope") == "shared" and labels.get("hermes-persist") == "0"
            and (item.get("status") or {}).get("state") != "running"):
        created = _parse_iso_utc(cfg.get("creationDate"))
        if created is None or (datetime.datetime.now(datetime.UTC) - created
                               ).total_seconds() < _SWEEP_STALE_AFTER_S:
            return None
        return "stale stopped non-persistent container"
    return None


def _is_idle_sandbox(item: dict, home: str, profile: str) -> bool:
    """Whether a listed container is a persistent sandbox of THIS Hermes home
    and profile that runs with no living user: what a Hermes process that
    died without cleanup leaves behind. The sweep stops it, never deletes it.

    The labels must say whose it is; one without them is not known to be
    this home's and is left alone."""
    cfg = item.get("configuration") or {}
    labels = cfg.get("labels") or {}
    if not _is_ours(labels):
        return False
    if labels.get("hermes-scope") != "shared" or labels.get("hermes-persist") != "1":
        return False
    if labels.get("hermes-home") != home or labels.get("hermes-profile") != profile:
        return False
    if (item.get("status") or {}).get("state") != "running":
        return False
    return bool(item.get("id"))  # who uses it is asked under its lock, by the stop


def sweep_orphans(exe: str, *, stop_idle: bool = True) -> int:
    """Clean up after crashed Hermes processes: remove THIS backend's leftover
    containers, and stop its idle persistent sandboxes.

    Only containers labelled ``hermes-agent=1`` AND ``hermes-backend=
    apple_container`` of this Hermes home and profile are ever considered.
    Removed: session-scoped ones whose owner process is dead, and stopped
    non-persistent shared ones older than _SWEEP_STALE_AFTER_S (a fresh one
    may belong to a sibling about to reuse it). Stopped, never deleted (and
    not at all with ``stop_idle=False``, the ``apple_container_keep_running``
    opt-out): a running persistent sandbox — it IS the user's state. Neither
    while a living process holds a lease on it. Best-effort: an entry that
    cannot be read is skipped, and no failure here ever fails the session."""
    removed = 0
    try:
        with _SWEEP_LOCK:
            if os.getpid() in _SWEEP_DONE:
                return 0
            _SWEEP_DONE.add(os.getpid())
        listing = run_capture([exe, "list", "--all", "--format", "json"], timeout=15)
        if listing.returncode != 0:
            logger.warning("apple_container: orphan sweep skipped — `container list` "
                           "exited %d: %s", listing.returncode, listing.stderr.strip()[:200])
            return 0
        home, profile = _home_key(), _sanitize_label(_profile_name())
        listed = json.loads(listing.stdout or "[]")
        for item in listed:
            try:
                if stop_idle and _is_idle_sandbox(item, home, profile):
                    if _stop_if_unused(exe, item["id"]):
                        logger.info("apple_container: stopped %s (a persistent sandbox no "
                                    "living Hermes process uses; it starts again on the "
                                    "next command)", item["id"])
                    continue
                reason = _sweep_reason(item, home, profile)
                if reason is None:
                    continue
                name = item["id"]
                running = (item.get("status") or {}).get("state") == "running"
                if _remove_container(exe, name, running=running):
                    removed += 1
                    _remove_staging(name)
                    logger.info("apple_container: swept %s (%s)", name, reason)
            except Exception:
                logger.warning("apple_container: orphan sweep skipped an entry it could "
                               "not read", exc_info=True)
        _sweep_staging({str(item.get("id")) for item in listed if isinstance(item, dict)})
    except Exception:
        logger.warning("apple_container: orphan sweep failed", exc_info=True)
    return removed


def _sweep_staging(existing: set[str]) -> None:
    """Remove staged credential copies whose sandbox no longer exists (it was
    deleted by hand, or its process crashed). A folder touched recently is
    kept: a sibling process may be about to start its sandbox."""
    try:
        folders = list((_state_dir() / "staged").iterdir())
    except OSError:
        return
    for folder in folders:
        try:
            age = datetime.datetime.now().timestamp() - folder.stat().st_mtime
            if (folder.name not in existing and age > _SWEEP_STALE_AFTER_S
                    and not _live_leases(folder.name)):
                _remove_staging(folder.name)
                logger.info("apple_container: removed staged files of %s (its sandbox "
                            "is gone)", folder.name)
        except OSError:
            continue


def _best_effort(fn, argv, **kwargs) -> bool:
    try:
        return fn(argv, **kwargs).returncode == 0
    except Exception:
        return False


def _parse_iso_utc(text: Any) -> Optional[datetime.datetime]:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


class AppleContainerEnvironment(BaseEnvironment):
    """Commands run inside one Apple container Linux VM, reused by name."""

    # A timed-out `sudo -n true` probe kills only its own exec, never the VM.
    _passwordless_sudo_probe_supported = True

    def __init__(self, *, exe: Optional[str], image: str, cwd: str, timeout: int,
                 task_id: str = "default", terminal_config: Optional[dict] = None,
                 container_config: Optional[dict] = None):
        terminal = terminal_config or {}
        cc = container_config or {}
        if cwd == "~" or not str(cwd).startswith("/"):
            cwd = "/root"  # the persistent volume's mount point
        super().__init__(cwd=cwd, timeout=timeout)
        self._task_id = task_id
        self._image = image
        # The plugin's own memory/cpus settings win over core's
        # container_config: Hermes routes the TERMINAL_CONTAINER_* env vars
        # through plumbing a multiplexed gateway does not reliably deliver
        # (measured), so the form is the dependable place to set resources.
        # 0 (the default) keeps what Hermes hands down.
        memory_override = terminal.get("apple_container_memory", 0)
        cpu_override = terminal.get("apple_container_cpus", 0)
        self._memory = (memory_override if isinstance(memory_override, int)
                        and not isinstance(memory_override, bool) and memory_override > 0
                        else cc.get("container_memory", 5120))
        self._cpu = (cpu_override if isinstance(cpu_override, int)
                     and not isinstance(cpu_override, bool) and cpu_override > 0
                     else cc.get("container_cpu", 1))
        self._disk = cc.get("container_disk", 51200)
        # container_persistent is core's terminal key (Docker uses it too),
        # handed on by core as container_config; a value saved in the form
        # (the resolver's apple_container_container_persistent) wins over it.
        form_persistent = terminal.get("apple_container_container_persistent")
        self._persistent = (bool(cc.get("container_persistent", True)) if form_persistent is None
                            else bool(form_persistent))
        self._session_scoped = (not self._persistent) and task_id != "default"
        self._mount_cwd = bool(terminal.get("apple_container_mount_cwd"))
        if self._mount_cwd:
            self.cwd = "/workspace"  # the host launch dir is mounted there
        try:
            self._user_mounts = parse_volumes(terminal.get("apple_container_volumes"))
            self._extra: ExtraArgs = parse_extra_args(terminal.get("apple_container_extra_args"))
        except SpecError as exc:
            raise AppleContainerConfigError(str(exc)) from exc
        self._forward_env = _forward_env_names(terminal.get("apple_container_forward_env"))
        self._literal_env = _literal_env(terminal.get("apple_container_env"))
        shm = str(terminal.get("apple_container_shm_size", DEFAULT_SHM_SIZE) or "").strip()
        self._shm_size = shm if shm and shm != "0" else ""
        user = str(terminal.get("apple_container_user", "") or "").strip()
        self._user = user or None
        self._network = str(terminal.get("apple_container_network", "default") or "default").strip()
        self._hermes_mount_kinds = hermes_mount_kinds(terminal)
        keep = terminal.get("apple_container_keep_running")
        if keep is not None and not isinstance(keep, bool):
            raise AppleContainerConfigError(
                f"apple_container_keep_running must be true or false, got {keep!r}")
        self._keep_running = bool(keep)

        self._exe = exe or self._ensure_available()
        # Leases, locks and staged files stay under the home this environment
        # was made in, whatever home is in effect when it is cleaned up.
        self._state_root = _state_dir()

        identity = container_identity(task_id, persistent=self._persistent,
                                      workspace=os.getcwd() if self._mount_cwd else "")
        self.container_name = f"hermes-ac-{identity}"
        self._volume_names = {
            "home": f"hermes-acv-{identity}-home",
            "workspace": f"hermes-acv-{identity}-ws",
        }
        self._mounts = self._build_mounts()
        self._warn_about_sensitive_shares()
        self.cwd = self._sandbox_cwd(self.cwd)
        task_label = _sanitize_label(task_id)
        # Settings `inspect` cannot give back in a comparable form: a changed
        # signature means the container was made with other settings.
        self._config_signature = hashlib.sha256(json.dumps(
            {"args": self._extra.args, "network": self._network}, sort_keys=True
        ).encode("utf-8")).hexdigest()[:16]
        self._labels = {
            "hermes-agent": "1",
            "hermes-backend": "apple_container",
            "hermes-profile": _sanitize_label(_profile_name()),
            "hermes-home": _home_key(),
            "hermes-task": task_label,
            "hermes-scope": "session" if self._session_scoped else "shared",
            "hermes-persist": "1" if self._persistent else "0",
            "hermes-config": self._config_signature,
        }
        if self._session_scoped:
            self._labels["hermes-owner"] = f"pid{os.getpid()}"
        self._lease: Optional[Path] = None

        try:
            # The lease first: the sweep below, and another process's cleanup,
            # then see this sandbox as in use and leave it running.
            self._acquire_lease()
            sweep_orphans(self._exe, stop_idle=not self._keep_running)
            self._stage_credentials()
            with _sandbox_lock(self.container_name, _ATTACH_LOCK_WAIT_S, self._state_root):
                self._attach_or_create()
            self._init_env_names, self._init_env_values = self._build_init_env()
            self.init_session()
        except BaseException:
            # A start that fails or is interrupted half-way must not leave a
            # session VM (or its lease) behind; a persistent one is kept.
            try:
                self.cleanup()
            except Exception:
                logger.debug("apple_container: cleanup after a failed start failed",
                             exc_info=True)
            raise

    # --- availability (fail closed) ---------------------------------------
    @staticmethod
    def _ensure_available() -> str:
        if reason := unsupported_host_reason():
            raise EnvironmentConnectionError(
                f"Apple Container backend selected but {reason}. Commands never fall "
                "back to running on this machine.",
                retry_hint="Switch backends with `hermes setup terminal`.")
        exe = find_container_executable()
        if not exe:
            raise EnvironmentConnectionError(
                "Apple Container backend selected but the `container` CLI was not found "
                "in PATH or the usual install locations. Install it from "
                "https://github.com/apple/container — commands never fall back to "
                "running on this Mac.",
                retry_hint="Install Apple's container CLI (github.com/apple/container) and "
                           "retry, or switch backends with `hermes setup terminal`.")
        try:
            probe = run_capture([exe, "system", "status"], timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentConnectionError(
                f"`{exe} system status` failed: {exc}. The Apple container service must "
                "be running; start it with `container system start --enable-kernel-install`.",
                retry_hint="Start the container system service and retry — recovery is "
                           "automatic once it is back.") from exc
        if probe.returncode != 0:
            raise EnvironmentConnectionError(
                "The Apple container system service is not running. Start it with "
                "`container system start --enable-kernel-install`.",
                retry_hint="Start the container system service and retry — recovery is "
                           "automatic once it is back.")
        return exe

    # --- mount / label construction ---------------------------------------
    def _build_mounts(self) -> list[Mount]:
        """The ``-v`` mounts, in order: persistent volumes first (so later
        nested read-only mounts land inside them), then user shares, the
        launch directory, and the read-only Hermes skill/credential/cache
        mounts.

        No two mounts may land on one path (the real CLI mounts both and the
        later one shadows the earlier). The user's own mounts — the volumes
        setting and mounts in the extra arguments — win over the ones the
        backend adds by itself; two of the user's own on one path are refused."""
        taken: dict[str, Mount] = {}
        for mount in (*self._user_mounts, *self._extra.mounts):
            if mount.target in taken:
                raise AppleContainerConfigError(
                    f"two mounts land on {mount.target} in the sandbox "
                    f"({taken[mount.target].spec()} and {mount.spec()}); the later one "
                    "would hide the earlier")
            if mount.kind == "host" and os.path.exists(mount.source) \
                    and not os.path.isdir(mount.source):
                # Measured: the runtime shares the file's whole parent folder
                # with the VM underneath a single-file mount, and a folder
                # shared together with one of its own files loses the folder.
                raise AppleContainerConfigError(
                    f"{mount.source} is a file, and a single file cannot be shared safely: "
                    "the container runtime would share its whole parent folder with the VM "
                    "underneath. Put the file in a folder of its own and share that folder "
                    f"(\"/that/folder:{posixpath.dirname(mount.target) or '/'}"
                    f"{':ro' if mount.readonly else ''}\")")
            taken[mount.target] = mount
        mounts: list[Mount] = []

        def add_own(mount: Mount, what: str) -> None:
            if mount.target in taken:
                logger.warning("apple_container: %s is not mounted: your settings already "
                               "mount %s at %s", what, taken[mount.target].source, mount.target)
                return
            taken[mount.target] = mount
            mounts.append(mount)

        if self._persistent:
            add_own(Mount("volume", self._volume_names["home"], "/root"),
                    "the persistent home volume")
            if not self._mount_cwd:  # the launch dir would own /workspace
                add_own(Mount("volume", self._volume_names["workspace"], "/workspace"),
                        "the persistent workspace volume")
        mounts.extend(self._user_mounts)
        if self._mount_cwd:
            add_own(Mount("host", os.getcwd(), "/workspace"), "the launch directory")
        folders, credentials = _hermes_file_shares(self._hermes_mount_kinds)
        for mount in folders:
            add_own(mount, "a Hermes file share")
        # Credential files are never mounted from where they are (the runtime
        # would share their whole folder — the Hermes home — underneath): each
        # is copied into a private folder of its own and mounted from there.
        stage_root = self._state_root / "staged" / self.container_name
        self._staged: list[tuple[str, Path]] = []
        for host_path, container_path in credentials:
            key = hashlib.sha256(container_path.encode("utf-8")).hexdigest()[:12]
            staged = stage_root / key / os.path.basename(host_path)
            before = len(mounts)
            add_own(Mount("host", str(staged), container_path, readonly=True),
                    "a Hermes credential file")
            if len(mounts) > before:
                self._staged.append((host_path, staged))
        return mounts

    def _stage_credentials(self) -> None:
        """Write the private copies the credential mounts point at; drop
        copies that are no longer wanted. Owner-only (0700 folders, 0600
        files), one folder per file, so each share holds that file alone."""
        root = self._state_root / "staged" / self.container_name
        if not self._staged:
            if root.exists():
                _remove_staging(self.container_name, self._state_root)
            return
        try:
            for folder in (self._state_root, root.parent, root):
                _private_dir(folder)
            for host_path, staged in self._staged:
                _private_dir(staged.parent)
                _copy_in_place(host_path, staged)
            wanted = {staged.parent.name for _, staged in self._staged}
            for entry in root.iterdir():
                if entry.name not in wanted:
                    shutil.rmtree(entry, ignore_errors=True)
            os.utime(root)  # fresh: the leftover sweep keeps it
        except OSError as exc:
            raise EnvironmentConnectionError(
                f"Apple Container backend: could not stage a credential file for the "
                f"sandbox: {exc}. No sandbox was started; commands never fall back to "
                "running on this Mac.",
                retry_hint="Check that the credential file is readable, or stop sharing "
                           "`credentials` in terminal.apple_container_mount_hermes_files.") from exc

    def _desired_mount_map(self) -> dict[str, tuple[str, bool]]:
        """destination -> (source-or-volume-name, read-only) for comparison,
        including the mounts the user passes through the extra arguments."""
        return {mount.target: (_mount_key(mount.kind, mount.source), mount.readonly)
                for mount in (*self._mounts, *self._extra.mounts)}

    def _warn_about_sensitive_shares(self) -> None:
        """Log a warning for every Mac folder shared with the sandbox that
        holds, or lies inside, a place where credentials usually live."""
        for mount in (*self._mounts, *self._extra.mounts):
            if mount.kind != "host" or mount.target.startswith("/root/.hermes"):
                continue  # Hermes' own file shares are an explicit setting of their own
            for place in _credential_places_in(mount.source):
                logger.warning(
                    "apple_container: the share %s -> %s gives the sandbox %s, where "
                    "credentials are usually kept (%s). A command in the sandbox can read "
                    "them and, with network access, send them anywhere.",
                    mount.source, mount.target, place,
                    "read-only" if mount.readonly else "READ-WRITE")

    def _sandbox_cwd(self, cwd: str) -> str:
        """A working directory that exists as a place in the sandbox: a path
        of the Mac (``/Users/...``) is replaced by the sandbox home, unless a
        mount puts something at that very path."""
        cwd = posixpath.normpath(cwd)
        if not cwd.startswith(_MAC_PATH_PREFIXES + (str(Path.home()) + "/",)) \
                and cwd not in (str(Path.home()), *(p.rstrip("/") for p in _MAC_PATH_PREFIXES)):
            return cwd
        for mount in (*self._mounts, *self._extra.mounts):
            if cwd == mount.target or cwd.startswith(mount.target + "/"):
                return cwd
        logger.info("apple_container: working directory %s is a path of the Mac; "
                    "using /root in the sandbox", cwd)
        return "/root"

    @staticmethod
    def _actual_mount_map(cfg: dict) -> Optional[dict[str, tuple[str, bool]]]:
        mounts = cfg.get("mounts")
        if not isinstance(mounts, list):
            return None  # inspect gave no mount data: no verdict, keep the container
        actual: dict[str, tuple[str, bool]] = {}
        for mount in mounts:
            if not isinstance(mount, dict):
                continue
            kinds = mount.get("type") or {}
            if "tmpfs" in kinds:
                continue  # not a share; covered by the extra-arguments signature
            dest = str(mount.get("destination", "")).rstrip("/") or "/"
            volume = kinds.get("volume") or {}
            if volume.get("name"):
                source = _mount_key("volume", str(volume["name"]))
            else:
                source = _mount_key("host", str(mount.get("source") or ""))
            # Only the exact option `ro` is read-only to the runtime.
            ro = "ro" in [str(o) for o in (mount.get("options") or [])]
            actual[dest] = (source, ro)
        return actual

    def _desired_networks(self) -> list[str]:
        """Network names the container should be attached to, the way
        ``inspect`` lists them: ``none`` is no network at all."""
        names = [str(n).split(",", 1)[0].strip() for n in (self._extra.networks or [self._network])]
        return [] if "none" in names else sorted(names)

    def _config_difference(self, cfg: dict) -> Optional[str]:
        """Reuse guard: what differs between an existing container and the
        request (image, mounts, network, extra arguments), or ``None``.

        The image is compared in the registry-qualified form ``inspect``
        reports (`debian` is `docker.io/library/debian:latest`). Missing
        inspect fields give no verdict (a failed probe must not churn a
        sandbox the user has state in)."""
        reference = ((cfg.get("image") or {}).get("reference")) or None
        if reference and not same_image(self._image, reference):
            return f"image ({reference} -> {self._image})"
        actual = self._actual_mount_map(cfg)
        if actual is not None and actual != self._desired_mount_map():
            return "mounts"
        networks = cfg.get("networks")
        if isinstance(networks, list):
            attached = sorted(str(n.get("network")) for n in networks if isinstance(n, dict))
            if attached != self._desired_networks():
                return f"network ({attached or ['none']} -> {self._desired_networks() or ['none']})"
        signature = (cfg.get("labels") or {}).get("hermes-config")
        if signature is not None and signature != self._config_signature:
            return "network options or extra arguments"
        return None

    # --- who uses the container ---------------------------------------------
    def _acquire_lease(self) -> None:
        """Mark this environment as a user of its container, so another
        environment's cleanup, or the sweep, neither stops nor deletes it."""
        if self._lease is not None:
            return
        try:
            folder = _lease_dir(self.container_name, self._state_root)
            folder.mkdir(parents=True, exist_ok=True)
            lease = folder / f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
            lease.write_text("", encoding="utf-8")
            self._lease = lease
        except OSError:
            logger.warning("apple_container: could not write a lease for %s",
                           self.container_name, exc_info=True)

    def _release_lease(self) -> None:
        lease, self._lease = getattr(self, "_lease", None), None
        if lease is None:
            return
        for step in (lease.unlink, lease.parent.rmdir):  # rmdir: only when empty
            try:
                step()
            except OSError:
                pass

    # --- container lifecycle ------------------------------------------------
    def _inspect(self, name: str) -> Optional[dict]:
        return _inspect_container(self._exe, name)

    def _attach_or_create(self) -> None:
        existing = self._inspect(self.container_name)
        if existing is not None:
            cfg = existing.get("configuration") or {}
            labels = cfg.get("labels") or {}
            if (labels.get("hermes-agent") != "1"
                    or labels.get("hermes-backend") != "apple_container"):
                raise EnvironmentConnectionError(
                    f"A container named {self.container_name} exists but is not one of "
                    f"this backend's (labels: {labels}). Refusing to touch it — remove it "
                    "yourself (`container delete " + self.container_name + "`) or change "
                    "the workspace. Commands never fall back to running on this Mac.")
            difference = self._config_difference(cfg)
            if difference:
                logger.warning(
                    "apple_container: container %s was made with other settings (%s) — "
                    "replacing it with a fresh one. Persistent volumes are kept.",
                    self.container_name, difference)
                self._delete_own_container()
            else:
                state = (existing.get("status") or {}).get("state")
                if state == "running":
                    logger.info("apple_container: reusing container %s", self.container_name)
                    return
                started = _best_effort(run_capture, [self._exe, "start", self.container_name],
                                       timeout=60)
                if started:
                    logger.info("apple_container: restarted container %s", self.container_name)
                    return
                logger.warning("apple_container: could not start existing container %s — "
                               "creating a fresh one", self.container_name)
                self._delete_own_container()
        if self._persistent:
            self._ensure_volumes()
        self._create_container()

    def _ensure_volumes(self) -> None:
        """Create the backend's own persistent volumes that are mounted (one
        the user's settings replaced is neither mounted nor created)."""
        own = set(self._volume_names.values())
        for mount in self._mounts:
            if mount.kind == "volume" and mount.source in own:
                self._ensure_volume(mount.source)

    def _ensure_volume(self, name: str) -> None:
        probe = run_capture([self._exe, "volume", "inspect", name], timeout=15)
        if probe.returncode == 0:
            return
        argv = [self._exe, "volume", "create",
                "--label", "hermes-backend=apple_container",
                "--label", f"hermes-task={self._labels['hermes-task']}"]
        if self._disk and self._disk > 0:
            argv += ["-s", f"{int(self._disk)}m"]
        argv.append(name)
        result = run_capture(argv, timeout=120)
        if result.returncode != 0:
            raise EnvironmentConnectionError(
                f"`container volume create {name}` failed (exit {result.returncode}): "
                f"{result.stderr.strip()[:300]}")

    def _run_argv(self) -> list[str]:
        # `--init`: the runtime's init is PID 1, reaps orphans and forwards
        # signals (a bare `sleep` does neither). `--entrypoint env`: the
        # keep-alive runs whatever ENTRYPOINT the image has; with `IMAGE sleep
        # infinity` alone, an image whose entrypoint is not a pass-through
        # would take `sleep infinity` as ITS arguments and exit.
        argv = [self._exe, "run", "-d", "--progress", "none", "--init",
                "--entrypoint", "env", "--name", self.container_name]
        for key, value in self._labels.items():
            argv += ["-l", f"{key}={value}"]
        argv += ["-w", self.cwd]
        if cpus := _format_cpus(self._cpu):
            argv += ["-c", cpus]
        if self._memory and self._memory > 0:
            argv += ["-m", f"{int(self._memory)}m"]
        if self._shm_size:
            argv += ["--shm-size", self._shm_size]
        if not self._extra.networks:  # a network in the extra arguments replaces ours
            argv += ["--network", self._network]
        for mount in self._mounts:
            argv += ["-v", mount.spec()]
        argv += self._extra.args
        argv += [self._image, "sleep", "infinity"]
        return argv

    def _create_container(self) -> None:
        argv = self._run_argv()
        logger.debug("apple_container: starting container: %s", " ".join(argv))
        try:
            result = run_capture(argv, timeout=900)  # the first run may pull the image
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise EnvironmentConnectionError(
                f"Could not start the Apple container sandbox: {exc}. "
                "Commands never fall back to running on this Mac.",
                retry_hint="Check the image reference and `container list --all` for "
                           "leftovers, then retry.") from exc
        if result.returncode != 0:
            # The CLI's own words are the diagnosis (unknown network, missing
            # folder, image pull denied): keep the end of what it printed.
            said = (result.stderr or result.stdout or "").strip()[-2000:] or "no output"
            # Measured on 1.2.2: a named volume is a disk image and cannot be
            # attached to two running VMs at once; a second sandbox starting
            # while another one holds the volume fails with Virtualization
            # framework's "storage device attachment is invalid". A Mac folder
            # (virtiofs) attaches to many, so it is the shape for caches
            # shared between concurrent per-session sandboxes.
            hint = ""
            if "storage device attachment is invalid" in said:
                hint = (" Hint: a named volume cannot be attached to two running "
                        "sandboxes at once — another sandbox is probably holding one "
                        "right now. Share a Mac folder instead of a named volume when "
                        "several sandboxes run at the same time.")
            raise EnvironmentConnectionError(
                f"Could not start the Apple container sandbox: `container run` exited "
                f"{result.returncode}: {said}. Commands never fall back to running on "
                f"this Mac.{hint}",
                retry_hint="Fix what the container CLI reported (image, network, a shared "
                           "folder that does not exist), then retry.")
        logger.info("apple_container: started container %s (image %s)",
                    self.container_name, self._image)

    def _delete_own_container(self) -> None:
        """Remove OUR container only (labels re-checked first): stop and
        delete, then kill and force-delete if it would not stop."""
        existing = self._inspect(self.container_name)
        if existing is None:
            return
        labels = (existing.get("configuration") or {}).get("labels") or {}
        if labels.get("hermes-backend") != "apple_container" or labels.get("hermes-agent") != "1":
            logger.error("apple_container: refusing to remove %s — labels %s are not ours",
                         self.container_name, labels)
            return
        running = (existing.get("status") or {}).get("state") == "running"
        if not _remove_container(self._exe, self.container_name, running=running):
            logger.error("apple_container: could not remove container %s; remove it with "
                         "`container delete --force %s`", self.container_name,
                         self.container_name)

    # --- environment forwarding -------------------------------------------
    def _build_init_env(self) -> tuple[tuple[str, ...], dict[str, str]]:
        """Names forwarded at the login exec that seeds the snapshot: TZ, the
        explicit allow-list, and literal pairs. Name-only ``-e KEY`` flags;
        values travel in the CLI subprocess env, never the command line."""
        values: dict[str, str] = {}
        if tz := _host_timezone():
            values["TZ"] = tz
        for name in self._forward_env:
            value = _forwarded_value(name)
            if value is not None:
                values[name] = value
            else:
                logger.info("apple_container: forward-env %s is not set; skipped", name)
        values.update(self._literal_env)
        return tuple(sorted(values)), values

    # --- command execution ---------------------------------------------------
    def _guest_timeout(self) -> bool:
        key = (self._exe, self._image)
        if key in _GUEST_TIMEOUT_CACHE:
            return _GUEST_TIMEOUT_CACHE[key]
        available = False
        try:
            probe = run_capture([self._exe, "exec", self.container_name,
                                 "bash", "-c", "command -v timeout"], timeout=15)
            available = probe.returncode == 0 and bool(probe.stdout.strip())
        except Exception:
            available = False
        _GUEST_TIMEOUT_CACHE[key] = available
        return available

    def _run_bash(self, cmd_string: str, *, login: bool = False, timeout: int = 120,
                  stdin_data: str | None = None) -> subprocess.Popen:
        """Spawn bash inside the VM via `container exec`. The wrapped script
        gets a unique token comment so a kill can find it with pkill in the
        guest; when the guest has `timeout`, the whole command additionally
        runs under an in-guest backstop that fires just after the host
        deadline (killing the host client orphans nothing)."""
        token = uuid.uuid4().hex[:12]
        cmd_string = f"{cmd_string}\n# {_EXEC_TOKEN_PREFIX}{token}"
        argv = [self._exe, "exec"]
        if stdin_data is not None:
            argv.append("-i")
        env_values: dict[str, str] = {}
        if login:
            for name in self._init_env_names:
                argv += ["-e", name]
            env_values = dict(self._init_env_values)
        if self._user:
            argv += ["-u", self._user]
        argv.append(self.container_name)
        inner = bash_argv(cmd_string, login)
        guest_timeout = bool(timeout and timeout > 0 and self._guest_timeout())
        if guest_timeout:
            argv += ["timeout", "-k", str(_GUEST_TIMEOUT_KILL_S),
                     str(int(timeout) + _GUEST_TIMEOUT_GRACE_S), *inner]
        else:
            argv += inner
        proc = _start_exec(argv, stdin_data, _client_env(env_values))
        proc._hermes_exec_token = token  # type: ignore[attr-defined]
        proc._hermes_guest_timeout = guest_timeout  # type: ignore[attr-defined]
        return proc

    def _kill_process(self, proc) -> None:
        """Kill the exec client, and the command inside the VM. With the
        in-guest ``timeout`` wrapper active, that backstop kills the whole
        process group just after the host deadline — the fallback pkill would
        only TERM the wrapper bash first and orphan the command, so it is used
        ONLY on images without ``timeout`` (where it at least stops the
        pipeline, with grandchildren possibly orphaned)."""
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        if getattr(proc, "_hermes_guest_timeout", False):
            return
        self._pkill_exec_token(proc)

    def _force_kill_process(self, proc) -> None:
        self._kill_process(proc)

    def _pkill_exec_token(self, proc) -> None:
        token = getattr(proc, "_hermes_exec_token", None)
        if not token:
            return
        # Bracket trick: the regex matches the target's literal token but not
        # this pkill command's own argv.
        pattern = f"{_EXEC_TOKEN_PREFIX}[{token[0]}]{token[1:]}"
        try:
            run_capture([self._exe, "exec", self.container_name,
                         "pkill", "-f", pattern], timeout=10)
        except Exception:
            logger.debug("apple_container: guest pkill failed", exc_info=True)

    # --- "container is gone" recovery ---------------------------------------
    def _is_container_gone(self, output: str) -> bool:
        """Whether a failed command's output is the CLI saying OUR container
        is missing or stopped. Measured on container 1.2.2:
        ``Error: get failed: container NAME not found`` and
        ``Error: container NAME is not running``. The name is part of the
        match so a command of the agent's that prints "not found" is not
        mistaken for it."""
        name = re.escape(self.container_name)
        return bool(re.search(
            rf"container (?:with ID )?{name} (?:not found|is not running)"
            rf"|container not found: {name}", output or ""))

    def _recover_container(self) -> bool:
        """Bring a missing or stopped container back; True when the failed
        command may be retried. False when the container is in fact running:
        the text then came from the command itself, which already ran and
        must not run twice."""
        try:
            # Under the sandbox's lock: a stop in progress finishes first.
            with _sandbox_lock(self.container_name, _ATTACH_LOCK_WAIT_S, self._state_root):
                if not self._restart_or_recreate():
                    return False
            self._snapshot_ready = False
            self.init_session()
            self._mark_recreated()
            return True
        except Exception:
            logger.error("apple_container: recovery failed", exc_info=True)
            return False

    def _restart_or_recreate(self) -> bool:
        existing = self._inspect(self.container_name)
        if existing is not None:
            labels = (existing.get("configuration") or {}).get("labels") or {}
            if (labels.get("hermes-backend") != "apple_container"
                    or labels.get("hermes-agent") != "1"):
                return False
            if (existing.get("status") or {}).get("state") == "running":
                return False
            logger.warning("apple_container: container %s is stopped — starting it",
                           self.container_name)
            return _best_effort(run_capture, [self._exe, "start", self.container_name],
                                timeout=60)
        logger.warning("apple_container: container %s is gone — creating it again",
                       self.container_name)
        if self._persistent:
            self._ensure_volumes()
        self._stage_credentials()
        self._create_container()
        return True

    def execute(self, command: str, cwd: str = "", **kwargs) -> dict:
        """Execute; if the VM was removed or stopped out-of-band (another
        process's idle stop, a user delete, a crash), bring it back from the
        same configuration and retry once. The CLI refuses a command on a
        stopped container before running any of it, so the retry is the
        command's first run."""
        self._acquire_lease()  # an environment used again after its cleanup is a user again
        result = super().execute(command, cwd, **kwargs)
        if (result.get("returncode", 0) != 0
                and self._is_container_gone(result.get("output", ""))
                and self._recover_container()):
            result = super().execute(command, cwd, **kwargs)
        return result

    # --- teardown ------------------------------------------------------------
    def cleanup(self):
        """This environment is done with its sandbox (idle for
        ``terminal.lifetime_seconds``, or its session ended).

        A persistent sandbox is STOPPED when no other living environment, of
        this or another Hermes process, uses it: the container and its
        volumes stay, the next command starts it again, and only processes
        left running inside are lost. ``apple_container_keep_running: true``
        leaves it running instead. A session/non-persistent sandbox is
        removed — ours only, labels re-checked — by the last environment
        that uses it."""
        name = getattr(self, "container_name", None)
        if name is None or not getattr(self, "_exe", None):
            return  # the constructor did not get as far as a container
        self._release_lease()
        if self._persistent:
            if self._keep_running:
                logger.info("apple_container: leaving container %s running "
                            "(apple_container_keep_running)", name)
            elif _stop_if_unused(self._exe, name, self._state_root):
                logger.info("apple_container: stopped idle container %s; the next command "
                            "starts it again", name)
            return
        others = _live_leases(name, self._state_root)
        if others:
            logger.info("apple_container: leaving container %s: %d other environment(s) "
                        "still use it", name, len(others))
            return
        self._delete_own_container()
        _remove_staging(name, self._state_root)  # the staged credential copies go with it

    def stop_idle(self) -> None:
        """Idle reclaim, as core names it for sandboxes it stops rather than
        tears down: the same as :meth:`cleanup` here."""
        self.cleanup()

    def exec_prefix(self, *, user: Optional[str] = None, interactive: bool = True) -> Optional[list[str]]:
        """Local argv that runs its remainder inside the VM with stdin/stdout
        open — lets Bot Desktop, ``computer_use`` and the browser follow the
        terminal into this backend. ``user`` is honoured when given."""
        argv = [self._exe, "exec", "-i"]
        if user or self._user:
            argv += ["-u", user or self._user]
        return [*argv, self.container_name]


def _profile_name() -> str:
    """Best-effort profile label for the container's label set, from the home
    path only (``hermes_constants`` is public plugin API): a
    ``profiles/<name>`` home is labelled ``<name>``, anything else (the default
    root or a throwaway test home) ``default``."""
    try:
        from hermes_constants import get_hermes_home
        parts = Path(get_hermes_home()).parts
        if len(parts) >= 2 and parts[-2] == "profiles":
            return _sanitize_label(parts[-1])
    except Exception:
        pass
    return "default"
