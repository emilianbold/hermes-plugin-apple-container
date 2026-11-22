"""The effective settings of the backend: the ONE place that reads them.

A setting can be written in two places of ``config.yaml``:

- ``plugins.entries.<plugin id>.settings.<key>`` — the plugin's own settings,
  declared in ``plugin.yaml`` (``config_schema``); this is what the form in
  Hermes Desktop (Settings > Plugins) edits;
- ``terminal.apple_container_<key>`` — the keys of earlier versions, which
  keep working.

:func:`resolve` is the only function that reads either. Environment creation,
the approval decision (``skip_container_guards``), ``cache_path_base`` and the
health checks all take its answer, so a folder shared through the form counts
for approvals exactly as one shared through ``terminal:`` does.

Precedence, per key: a value in the plugin's own settings wins — also an
empty one, so what the form shows as saved is what is in effect; otherwise
the ``terminal`` key; otherwise the default.

Fail closed: when the config cannot be read, or a value cannot be validated,
the answer carries ``error`` and no values. Every consumer then keeps
approval prompts on and refuses to start a sandbox. Nothing is skipped
silently — an unknown key is refused too (a misspelt ``network: none`` must
not leave the network on).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .specs import SpecError, parse_extra_args, parse_volumes

logger = logging.getLogger(__name__)

#: Every setting is handed on under its ``terminal`` name.
PREFIX = "apple_container_"

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Hermes' own host files that can be shared read-only with the sandbox, in
#: the order they are mounted.
HERMES_MOUNT_KINDS = ("credentials", "skills", "cache")

#: The options of the form: key in the plugin's settings -> type. The same
#: list as ``config_schema`` in plugin.yaml (a test compares the two).
FORM_KEYS: dict[str, type] = {
    "image": str,
    "volumes": list,
    "mount_cwd": bool,
    "mount_hermes_files": list,
    "forward_env": list,
    "env": dict,
    "network": str,
    "user": str,
    "shm_size": str,
    "keep_running": bool,
    "container_persistent": bool,
    "memory": int,
    "cpus": int,
    "extra_args": list,
}

#: Form keys whose older spelling is a CORE terminal key without this
#: plugin's prefix — ``terminal.container_persistent`` (the Docker backend
#: uses it too), which core hands to the environment as container_config.
#: The resolver reads these from the form only, so no third
#: ``terminal.apple_container_<key>`` spelling exists; when the form has no
#: value, the core key keeps its meaning unchanged.
CORE_TWIN_KEYS = frozenset({"container_persistent"})

#: Options that exist as ``terminal.apple_container_<key>`` only: which
#: program is run on the Mac is not something a form should change.
TERMINAL_ONLY_KEYS = ("binary",)

_TYPE_WORDS = {str: "text", bool: "true or false", list: "a list", dict: "a mapping",
               int: "a whole number"}

# The most a resource setting may ask for: 4 TB of memory (in MB), 256 cores. A larger number
# is a slip of units, refused instead of handed to the runtime.
RESOURCE_LIMITS = {"memory": (4 * 1024 * 1024, "MB"), "cpus": (256, "cores")}

# The registry id Hermes stores this plugin's settings under; register()
# replaces it with the id the loader actually gave the plugin.
_plugin_id = "apple_container"

# Conflict warnings already logged (the approval decision asks often).
_WARNED: set[tuple] = set()


def set_plugin_id(plugin_id: Any) -> None:
    global _plugin_id
    if isinstance(plugin_id, str) and plugin_id.strip():
        _plugin_id = plugin_id.strip()


def form_place(key: str = "") -> str:
    """Where a form setting lives in config.yaml, as a dotted path."""
    return f"plugins.entries.{_plugin_id}.settings" + (f".{key}" if key else "")


@dataclass(frozen=True)
class Settings:
    """What :func:`resolve` found. ``values`` are the settings that are set,
    under their ``terminal`` names (``apple_container_volumes`` ...);
    ``sources`` says where each came from; ``error`` is why the settings
    cannot be used at all (``values`` is then empty)."""

    values: dict[str, Any] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None


class _Refused(Exception):
    """A setting that cannot be read or validated."""


def resolve() -> Settings:
    """The effective settings, or the reason they are refused."""
    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception as exc:
        logger.debug("apple_container: could not load config.yaml", exc_info=True)
        return Settings(error=f"config.yaml could not be read ({exc})")
    read_error = getattr(config, "read_error", None)  # the FailedConfigRead fallback
    if read_error is not None:
        return Settings(error=f"config.yaml could not be read ({read_error})")
    if not isinstance(config, Mapping):
        config = {}
    try:
        return _resolve_from(config)
    except _Refused as exc:
        return Settings(error=str(exc))


def _resolve_from(config: Mapping) -> Settings:
    terminal = config.get("terminal")
    terminal = terminal if isinstance(terminal, Mapping) else {}
    form = _form_settings(config)
    known = (*(k for k in FORM_KEYS if k not in CORE_TWIN_KEYS), *TERMINAL_ONLY_KEYS)
    for name in terminal:
        if isinstance(name, str) and name.startswith(PREFIX) and name[len(PREFIX):] not in known:
            raise _Refused(f"terminal.{name} is not a setting of this backend (known: "
                           f"{', '.join(PREFIX + key for key in known)})")
    for key in known:
        if PREFIX + key in terminal and terminal[PREFIX + key] is None:
            raise _Refused(_no_value(f"terminal.{PREFIX}{key}"))
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for key, kind in FORM_KEYS.items():
        name = PREFIX + key
        if key in form:
            values[name] = _checked_form_value(key, kind, form[key])
            sources[name] = form_place(key)
            if key not in CORE_TWIN_KEYS and name in terminal and terminal[name] != form[key]:
                _warn_once(key, form[key], terminal[name])
        elif key not in CORE_TWIN_KEYS and name in terminal:
            values[name] = terminal[name]
            sources[name] = f"terminal.{name}"
    for key in TERMINAL_ONLY_KEYS:
        name = PREFIX + key
        if name in terminal:
            values[name] = terminal[name]
            sources[name] = f"terminal.{name}"
    _check_effective(values, sources)
    return Settings(values=values, sources=sources)


def _form_settings(config: Mapping) -> Mapping:
    """``plugins.entries.<id>.settings``; ``{}`` when nothing was saved there.
    A block of another shape, or a key the schema does not have, is refused."""
    block: Any = config
    for depth, part in enumerate(("plugins", "entries", _plugin_id, "settings")):
        if part not in block or block[part] is None:
            return {}
        block = block[part]
        if not isinstance(block, Mapping):
            where = ".".join(("plugins", "entries", _plugin_id, "settings")[:depth + 1])
            raise _Refused(f"{where} in config.yaml must be a mapping, got {block!r}")
    for key in block:
        if key not in FORM_KEYS:
            raise _Refused(f"{form_place(str(key))} is not a setting of this backend (known: "
                           f"{', '.join(FORM_KEYS)})")
    return block


def _checked_form_value(key: str, kind: type, value: Any) -> Any:
    """A value saved in the plugin's own settings, or :class:`_Refused`.
    Hermes only warns when such a value does not match the schema."""
    place = form_place(key)
    if value is None:
        raise _Refused(_no_value(place))
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise _Refused(f"{place} must be {_TYPE_WORDS[kind]}, got {value!r}")
    if key == "mount_hermes_files":
        for entry in value:
            if not isinstance(entry, str) or entry not in HERMES_MOUNT_KINDS:
                raise _Refused(f"{place}: {entry!r} is not one of "
                               f"{', '.join(HERMES_MOUNT_KINDS)}")
    elif key == "forward_env":
        for entry in value:
            if not isinstance(entry, str) or not ENV_NAME_RE.match(entry):
                raise _Refused(f"{place}: {entry!r} is not the name of an environment "
                               "variable")
    elif key == "env":
        for name, item in value.items():
            if not isinstance(name, str) or not ENV_NAME_RE.match(name):
                raise _Refused(f"{place}: {name!r} is not the name of an environment "
                               "variable")
            if item is not None and not isinstance(item, (str, int, float, bool)):
                raise _Refused(f"{place}: the value of {name} must be text or a number, "
                               f"got {item!r}")
    return value


def _no_value(place: str) -> str:
    """A key that is present and empty is not read as "not set": that is
    what `hermes config set <key> none` leaves behind (a YAML null, not the
    text "none"), and the network would stay on for a user who just cut it."""
    return (f"{place} has no value; give it one or remove the key (`hermes config set` "
            'turns the word none into an empty value: for the network "none", write it in '
            "config.yaml itself, in quotes, or in the form in Hermes Desktop)")


def _check_effective(values: Mapping[str, Any], sources: Mapping[str, str]) -> None:
    """Rules that hold wherever a setting was written: the two switches are
    booleans (the text "false" is not false), and shares and extra arguments
    pass the same parser the sandbox is built with."""
    for key in ("mount_cwd", "keep_running"):
        name = PREFIX + key
        if name in values and not isinstance(values[name], bool):
            raise _Refused(f"{sources[name]} must be true or false, got {values[name]!r}")
    for key, (most, unit) in RESOURCE_LIMITS.items():
        # Checked here, so the same rule holds wherever the value was written.
        name = PREFIX + key
        if name in values:
            value = values[name]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= most:
                raise _Refused(f"{sources[name]} must be a whole number of {unit} from 0 "
                               f"(Hermes' own value) to {most}, got {value!r}")
    for key, parse in (("volumes", parse_volumes), ("extra_args", parse_extra_args)):
        name = PREFIX + key
        if name in values:
            try:
                parse(values[name])
            except SpecError as exc:
                raise _Refused(f"{sources[name]} is refused: {exc}") from exc


def _warn_once(key: str, form_value: Any, terminal_value: Any) -> None:
    try:
        from hermes_constants import get_hermes_home
        home = str(get_hermes_home())
    except Exception:
        home = ""
    seen = (home, key, repr(form_value), repr(terminal_value))
    if seen in _WARNED:
        return
    _WARNED.add(seen)
    logger.warning(
        "apple_container: %s is set in two places. %s = %r is used; terminal.%s%s = %r is "
        "not. Remove one of them.", key, form_place(key), form_value, PREFIX, key,
        terminal_value)
