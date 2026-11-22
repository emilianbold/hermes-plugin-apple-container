"""The ``apple_container`` terminal backend provider: classification, health
checks and environment creation, all on the answer of the one settings
resolver (``settings.py``).

Hermes stays on the Mac; every command the agent runs executes inside an
Apple container Linux VM (github.com/apple/container). The sandbox shares
NOTHING of the Mac by default — folder shares are opt-in per folder
(``host:container[:ro]``), in the plugin's own settings or in
``terminal.apple_container_volumes``.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from typing import Any, Optional

from agent.terminal_env_provider import TerminalEnvironmentProvider

from . import settings as settings_mod
from .settings import HERMES_MOUNT_KINDS  # noqa: F401  (part of this module's surface)
from .specs import extra_args_host_access, volumes_share_host

logger = logging.getLogger(__name__)

_INSTALL_URL = "https://github.com/apple/container"
_SYSTEM_START_HINT = "container system start --enable-kernel-install"

# Same default sandbox image the built-in Docker backend uses (Python, Node,
# a shell, and the desktop stack for Bot Screen).
_DEFAULT_IMAGE = "nousresearch/hermes-sandbox:desktop"

# Locations checked when `container` is not on PATH (Apple's installer).
_SEARCH_PATHS = ("/usr/local/bin/container", "/opt/homebrew/bin/container")


def unsupported_host_reason() -> Optional[str]:
    """Why this machine cannot run Apple's ``container`` at all, or ``None``.
    It needs macOS on Apple silicon; a ``container`` binary that happens to be
    on the PATH of another system is not it."""
    system = platform.system()
    if system != "Darwin":
        return f"Apple Container needs macOS on an Apple silicon Mac (this is {system})"
    if platform.machine() != "arm64" and not _translated_by_rosetta():
        return ("Apple Container needs an Apple silicon Mac "
                f"(this one reports {platform.machine()})")
    return None


# What every program this plugin starts is given: the `container` CLI is not part of Hermes, and
# Hermes' own environment holds every provider key loaded from the profile's .env. So it gets
# this short list instead of a copy: the basics, the locale and time zone, the proxy settings
# the CLI uses to pull images, and the CLI's own `CONTAINER_*` variables.
_CLI_ENV_NAMES = frozenset({
    "PATH", "HOME", "TMPDIR", "USER", "LOGNAME", "SHELL", "LANG", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
})
_CLI_ENV_PREFIXES = ("LC_", "CONTAINER_")


def cli_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    """The environment for one `container` call. *extra* carries the values of variables the
    user chose to forward into the sandbox, for the one call that names them with `-e NAME`."""
    env = {name: value for name, value in os.environ.items()
           if name in _CLI_ENV_NAMES or name.startswith(_CLI_ENV_PREFIXES)}
    if extra:
        env.update(extra)
    return env


def _translated_by_rosetta() -> bool:
    """True for an Intel build of Python running on Apple silicon."""
    try:
        probe = subprocess.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
                               capture_output=True, text=True, timeout=5,
                               stdin=subprocess.DEVNULL, check=False, env=cli_env())
        return probe.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def find_container_executable() -> Optional[str]:
    """The ``container`` CLI: ``terminal.apple_container_binary`` from
    config.yaml, else the ``HERMES_APPLE_CONTAINER_BINARY`` variable, else
    ``container`` on PATH, then the usual install locations; ``None`` when
    absent.

    An explicit path that is not an executable file is honoured as
    "not found" (logged) rather than silently falling back to another binary:
    a stale setting must fail closed, not quietly use a different runtime."""
    override, source = "", "terminal.apple_container_binary"
    configured = settings_mod.resolve().values.get("apple_container_binary")
    if configured:
        override = os.path.expanduser(str(configured).strip())
    if not override:
        override, source = os.getenv("HERMES_APPLE_CONTAINER_BINARY") or "", \
            "HERMES_APPLE_CONTAINER_BINARY"
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        logger.error("%s=%r is not an executable file; treating the container CLI as "
                     "missing.", source, override)
        return None
    if found := shutil.which("container"):
        return found
    return next((p for p in _SEARCH_PATHS if os.path.isfile(p) and os.access(p, os.X_OK)), None)


def hermes_mount_kinds(terminal: dict[str, Any]) -> tuple[str, ...]:
    """Which of Hermes' own files ``apple_container_mount_hermes_files`` shares.

    Unset or ``false`` shares none (the default: the sandbox gets nothing of
    the Mac). ``true`` shares all of :data:`HERMES_MOUNT_KINDS`; a list picks
    some of them. Unknown names are ignored (logged), never widened."""
    value = terminal.get("apple_container_mount_hermes_files")
    if value is True:
        return HERMES_MOUNT_KINDS
    if value is None or value is False or value == "" or value == []:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        logger.warning("apple_container_mount_hermes_files is neither a boolean nor a list: %r", value)
        return ()
    wanted = set()
    for entry in value:
        name = str(entry).strip().lower()
        if name in HERMES_MOUNT_KINDS:
            wanted.add(name)
        else:
            logger.warning("Ignoring unknown apple_container_mount_hermes_files entry: %r "
                           "(known: %s)", entry, ", ".join(HERMES_MOUNT_KINDS))
    return tuple(kind for kind in HERMES_MOUNT_KINDS if kind in wanted)


def _host_shares_configured(terminal: dict[str, Any]) -> bool:
    """True when anything of the Mac is reachable from the sandbox: a user
    volume whose source is a Mac folder, the launch directory mounted to
    /workspace, Hermes' own skills, cache or credential files (unlike in the
    built-in Docker backend: a credential file the sandbox can read is one a
    command can send anywhere), or a mount or the SSH agent passed through
    ``apple_container_extra_args``.

    The verdict comes from the same parser the environment mounts with
    (``specs.py``); whatever that parser cannot classify counts as shared."""
    if terminal.get("apple_container_mount_cwd"):
        return True
    if hermes_mount_kinds(terminal):
        return True
    if volumes_share_host(terminal.get("apple_container_volumes")):
        return True
    return extra_args_host_access(terminal.get("apple_container_extra_args"))


def _shares_summary(resolved: "settings_mod.Settings") -> str:
    """What of the Mac the effective settings give the sandbox, and where
    each such setting is written (the Desktop form shows only its own place)."""
    shared = []
    for name in ("apple_container_volumes", "apple_container_mount_cwd",
                 "apple_container_mount_hermes_files", "apple_container_extra_args"):
        if name in resolved.values and _host_shares_configured({name: resolved.values[name]}):
            shared.append(f"{resolved.sources.get(name, name)} = {resolved.values[name]!r}")
    if not shared:
        return "nothing of this Mac is shared; approval prompts are skipped"
    return "shared with the sandbox, approval prompts stay on: " + "; ".join(shared)


def _env_image() -> str:
    try:
        from hermes_cli.config import get_env_value
        value = get_env_value("TERMINAL_APPLE_CONTAINER_IMAGE")
    except Exception:
        value = os.getenv("TERMINAL_APPLE_CONTAINER_IMAGE")
    return str(value or "").strip()


def _configured_image(terminal: dict[str, Any]) -> str:
    value = terminal.get("apple_container_image")
    if value is None or not str(value).strip():
        value = _env_image()
    return str(value or "").strip() or _DEFAULT_IMAGE


class AppleContainerProvider(TerminalEnvironmentProvider):
    """Apple container: a lightweight Linux VM per container, on this Mac."""

    # Commands run in the VM's own filesystem, not on the Mac: host OS/home/cwd
    # hints are suppressed, host-looking cwds are sanitized, file tools use
    # container path resolution.
    is_remote = True
    is_container = True
    # The sandbox is resumed by name (deterministic container name): with
    # terminal.container_persistent: false, each session must get its own
    # identity or two ephemeral runs would destroy each other's container.
    session_isolated_when_nonpersistent = True
    # The environment provides exec_prefix(): Bot Desktop, computer_use and
    # the browser can follow the terminal into the VM (needs a desktop-capable
    # image such as the default hermes-sandbox:desktop).
    supports_streams = True

    @property
    def name(self) -> str:
        return "apple_container"

    @property
    def display_name(self) -> str:
        return "Apple Container"

    @property
    def description(self) -> str:
        return "Lightweight Linux VM sandbox via Apple's container CLI (shares nothing of the Mac by default)."

    @property
    def env_description(self) -> str:
        return "an Apple container Linux VM (isolated from this Mac)"

    @property
    def cache_path_base(self) -> Optional[str]:
        # Hermes rewrites ~/.hermes/cache paths to this base when it names a
        # cached file to the agent. The cache is only inside the VM (read-only,
        # at /root/.hermes) while the user shares it; otherwise advertise no
        # base rather than a path that does not exist in the sandbox.
        resolved = settings_mod.resolve()
        if resolved.error is None and "cache" in hermes_mount_kinds(resolved.values):
            return "/root/.hermes"
        return None

    @property
    def default_cwd(self) -> Optional[str]:
        # The VM's home: the persistent volume is mounted there.
        return "/root"

    @property
    def strip_env_keys(self) -> frozenset:
        # The container CLI needs no credentials; nothing to strip.
        return frozenset()

    @property
    def skip_container_guards(self) -> bool:
        """Dangerous-command approvals are skipped only while NOTHING of the
        Mac is reachable from the sandbox (the same rule the built-in Docker
        backend applies via ``has_host_access``). Any host share — read-write
        or read-only, from the volumes setting or the extra arguments — a
        setting that cannot be classified, or settings the resolver refuses
        (an unreadable config, an invalid value) keep the approval layer ON.
        It makes no difference where a share was written: the decision is
        taken on the resolver's answer, the same one the sandbox is built
        from."""
        try:
            resolved = settings_mod.resolve()
            if resolved.error is not None:
                return False  # fail closed: the settings could not be read or validated
            return not _host_shares_configured(resolved.values)
        except Exception:
            return False

    def resolve_image(self, image: Optional[str],
                      terminal: Optional[dict[str, Any]] = None) -> str:
        """Image for a new container: the per-task override Hermes passes
        (batch runs), else the image setting (the plugin's own settings, then
        ``terminal.apple_container_image``), else
        ``TERMINAL_APPLE_CONTAINER_IMAGE``, else the default sandbox image.
        *terminal* is the resolver's effective settings; asked for when not
        given."""
        override = str(image or "").strip()
        if override:
            return override
        return _configured_image(terminal if terminal is not None
                                 else settings_mod.resolve().values)

    def is_available(self) -> bool:
        return unsupported_host_reason() is None and find_container_executable() is not None

    def _service_running(self) -> tuple[bool, str]:
        exe = find_container_executable()
        if not exe:
            return False, "not-found"
        try:
            probe = subprocess.run([exe, "system", "status"], capture_output=True, text=True,
                                   timeout=10, stdin=subprocess.DEVNULL, check=False,
                                   env=cli_env())
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error("apple_container: `%s system status` failed: %s", exe, exc)
            return False, f"probe-error: {exc}"
        if probe.returncode != 0:
            logger.error("apple_container: the container system service is not running "
                         "(`%s system status` exited %d). Start it with: %s",
                         exe, probe.returncode, _SYSTEM_START_HINT)
            return False, "stopped"
        return True, ""

    def check_requirements(self, config: dict[str, Any]) -> bool:
        if reason := unsupported_host_reason():
            logger.error("Apple Container backend selected but %s.", reason)
            return False
        if error := settings_mod.resolve().error:
            logger.error("Apple Container backend: %s. No sandbox is started until the "
                         "setting is fixed.", error)
            return False
        if not self.is_available():
            logger.error("Apple Container backend selected but the `container` CLI was not found "
                         "in PATH or the usual install locations. Install it: %s", _INSTALL_URL)
            return False
        ok, _ = self._service_running()
        return ok

    def probe(self) -> tuple[str, str]:
        if reason := unsupported_host_reason():
            return ("unavailable", f"{reason}.")
        if not self.is_available():
            return ("needs_setup", f"Apple's `container` CLI not found. Install: {_INSTALL_URL}")
        ok, detail = self._service_running()
        if not ok:
            return ("unavailable", "The container system service is not running. "
                                   f"Start it with: `{_SYSTEM_START_HINT}`")
        if error := settings_mod.resolve().error:
            return ("needs_setup", f"A setting is refused: {error}.")
        return ("ready", "")

    def setup_instructions(self) -> list[str]:
        if reason := unsupported_host_reason():
            return [f"{reason}."]
        exe = find_container_executable()
        lines = [f"Found `container` CLI at {exe}." if exe
                 else f"Apple's `container` CLI not found! Install it from {_INSTALL_URL}."]
        lines.append("The container system service must be running; start it with: "
                     f"`{_SYSTEM_START_HINT}`.")
        resolved = settings_mod.resolve()
        if resolved.error:
            lines.append(f"A setting is refused, and no sandbox starts until it is fixed: "
                         f"{resolved.error}.")
        lines.append(f"Image: {_configured_image(resolved.values)} (change it in Hermes Desktop "
                     "under Settings > Plugins, or with `hermes config set "
                     "terminal.apple_container_image <image>`).")
        lines.append("By default the sandbox shares NO folder of this Mac. To share one, set "
                     "terminal.apple_container_volumes (e.g. [\"/Users/you/project:/project\"] "
                     "or \"/data:/data:ro\" for read-only; absolute paths or `~`, folders only).")
        return lines

    def doctor_checks(self) -> list[tuple[bool, str, str]]:
        if reason := unsupported_host_reason():
            return [(False, "Apple Container backend", f"({reason})")]
        exe = find_container_executable()
        service_ok, _ = self._service_running()
        ok = bool(exe) and service_ok
        if exe:
            state = "running" if service_ok else f"NOT running — run `{_SYSTEM_START_HINT}`"
            detail = f"({exe}, service {state})"
        else:
            detail = "(container CLI not found)"
        rows = [(ok, "Apple Container backend", detail)]
        resolved = settings_mod.resolve()
        if resolved.error:
            rows.append((False, "Apple Container settings",
                         f"({resolved.error}; no sandbox starts until it is fixed)"))
        elif exe and service_ok:
            rows.append((True, "Apple Container image",
                         f"({self.resolve_image(None, resolved.values)})"))
            rows.append((True, "Apple Container shares", f"({_shares_summary(resolved)})"))
        return rows

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: Optional[str] = None,
                           container_config: Optional[dict[str, Any]] = None,
                           **kwargs: Any):
        from .apple_container_env import AppleContainerConfigError, AppleContainerEnvironment
        resolved = settings_mod.resolve()
        if resolved.error is not None:
            # Fail closed: settings that cannot be read or validated start nothing.
            raise AppleContainerConfigError(resolved.error)
        cc = container_config or {}
        return AppleContainerEnvironment(
            exe=None,  # resolved (and fail-closed) inside, after config is known
            image=self.resolve_image(image, resolved.values),
            cwd=cwd, timeout=timeout, task_id=task_id,
            terminal_config=resolved.values, container_config=cc)
