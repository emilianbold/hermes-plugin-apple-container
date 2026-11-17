"""What a setting means to Apple's ``container`` CLI: volume specs, extra
``container run`` arguments and image references.

This is the ONE place that decides what a spec is. The provider uses it for
the approval decision ("is anything of the Mac reachable from the sandbox?")
and the environment uses it for what is actually mounted, so the two can
never disagree. Rules were measured on container 1.2.2 (see README).

Fail closed: whatever cannot be classified counts as a host share (approval
prompts stay on) and is refused when the sandbox is created.
"""

from __future__ import annotations

import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


class SpecError(ValueError):
    """A setting that is refused; the message says which one and why."""


# A named volume is a bare name. Anything with path syntax is a Mac path to
# the CLI: `./d`, `../d`, `d/`, `.`, `~/d` all mount a Mac folder (measured).
_VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Mount:
    """One mount of the sandbox. ``kind`` is ``"host"`` (a folder of the Mac,
    ``source`` is its absolute path) or ``"volume"`` (a named volume, an ext4
    disk image that shares no Mac folder)."""

    kind: str
    source: str
    target: str
    readonly: bool = False

    def spec(self) -> str:
        """The ``-v`` argument: ``source:target`` plus ``:ro``, the only suffix
        the real CLI honours (``:readonly``, ``:RO`` mount READ-WRITE)."""
        return f"{self.source}:{self.target}" + (":ro" if self.readonly else "")


def _source(text: str, what: str, *, expand_tilde: bool) -> tuple[str, str]:
    """(kind, source) for the left side of a spec."""
    if not text:
        raise SpecError(f"{what}: the source is empty (the CLI would create an anonymous "
                        "volume); write `/absolute/mac/folder:/path` or `volume-name:/path`")
    if text.startswith("~"):
        if not expand_tilde:
            raise SpecError(f"{what}: write the absolute path instead of {text!r} here")
        expanded = os.path.expanduser(text)
        if not os.path.isabs(expanded):
            raise SpecError(f"{what}: cannot expand {text!r} to an absolute Mac path")
        return "host", os.path.normpath(expanded)
    if text.startswith("/"):
        return "host", os.path.normpath(text)
    if _VOLUME_NAME_RE.match(text):
        return "volume", text
    raise SpecError(
        f"{what}: the source {text!r} is neither an absolute Mac path nor a volume name. "
        "A relative path would be shared from whatever directory Hermes was started in; "
        "write the absolute path (`~` is accepted)")


def _target(text: str, what: str) -> str:
    if not text.startswith("/"):
        raise SpecError(f"{what}: the path inside the sandbox must be absolute, got {text!r}")
    if ".." in text.split("/"):
        raise SpecError(f"{what}: the path inside the sandbox may not contain `..`: {text!r}")
    target = posixpath.normpath(text)
    target = "/" + target.lstrip("/")  # normpath keeps a leading `//`
    if target == "/":
        raise SpecError(f"{what}: the sandbox's root cannot be a mount target")
    return target


def parse_volume(spec: Any, *, what: str = "apple_container_volumes",
                 expand_tilde: bool = True) -> Mount:
    """``source:target[:ro|:rw]`` as a :class:`Mount`, or :class:`SpecError`."""
    if not isinstance(spec, str):
        raise SpecError(f"{what}: every entry must be a string like "
                        f"\"/mac/folder:/path[:ro]\", got {spec!r}")
    text = spec.strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise SpecError(f"{what}: control character in {spec!r}")
    parts = text.split(":")
    if len(parts) < 2:
        raise SpecError(f"{what}: {spec!r} has no `:`; write `source:/path/in/sandbox[:ro]`")
    if len(parts) > 3:
        raise SpecError(f"{what}: {spec!r} has too many `:` (paths containing `:` cannot "
                        "be shared)")
    readonly = False
    if len(parts) == 3:
        if parts[2] not in ("ro", "rw"):
            raise SpecError(
                f"{what}: {spec!r} ends in `:{parts[2]}`. Only `:ro` (read-only) and `:rw` "
                "are accepted: the CLI silently mounts any other suffix READ-WRITE")
        readonly = parts[2] == "ro"
    what_spec = f"{what} {spec!r}"
    kind, source = _source(parts[0], what_spec, expand_tilde=expand_tilde)
    return Mount(kind, source, _target(parts[1], what_spec), readonly)


def parse_volumes(value: Any, *, what: str = "apple_container_volumes") -> list[Mount]:
    """Every entry of a volumes list; ``None`` is an empty list. One bad entry
    refuses the whole setting (a share silently left out, or mounted with
    other rights than written, is worse than an error)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SpecError(f"{what} must be a list of \"source:/path[:ro]\" strings, got {value!r}")
    return [parse_volume(entry, what=what) for entry in value]


def volume_is_host_share(spec: Any) -> bool:
    """True unless *spec* is certainly a named volume."""
    try:
        return parse_volume(spec).kind == "host"
    except SpecError:
        return True


def volumes_share_host(value: Any) -> bool:
    """True when a volumes setting reaches (or may reach) a Mac folder."""
    if value is None:
        return False
    if not isinstance(value, list):
        return True
    return any(volume_is_host_share(entry) for entry in value)


# --- extra `container run` arguments ---------------------------------------
# Flags of `container run` (1.2.2 --help): canonical name -> takes a value.
_RUN_FLAGS: dict[str, bool] = {
    "--env": True, "--env-file": True, "--gid": True, "--interactive": False, "--tty": False,
    "--user": True, "--uid": True, "--workdir": True, "--ulimit": True, "--cpus": True,
    "--memory": True, "--arch": True, "--cap-add": True, "--cap-drop": True, "--cidfile": True,
    "--detach": False, "--dns": True, "--dns-domain": True, "--dns-option": True,
    "--dns-search": True, "--entrypoint": True, "--init": False, "--init-image": True,
    "--kernel": True, "--kernel-arg": True, "--label": True, "--masked-path": True,
    "--mount": True, "--name": True, "--network": True, "--no-dns": False, "--os": True,
    "--publish": True, "--platform": True, "--publish-socket": True, "--read-only": False,
    "--read-only-path": True, "--remove": False, "--rosetta": False, "--runtime": True,
    "--ssh": False, "--shm-size": True, "--tmpfs": True, "--virtualization": False,
    "--volume": True, "--scheme": True, "--progress": True, "--max-concurrent-downloads": True,
    "--debug": False, "--version": False, "--help": False,
}
_ALIASES = {
    "-e": "--env", "-i": "--interactive", "-t": "--tty", "-u": "--user", "-w": "--workdir",
    "--cwd": "--workdir", "-c": "--cpus", "-m": "--memory", "-a": "--arch", "-d": "--detach",
    "-k": "--kernel", "-l": "--label", "-p": "--publish", "--rm": "--remove", "-v": "--volume",
    "-h": "--help",
}
# The backend owns the container's name, lifetime and keep-alive process.
_OWNED = {
    "--name": "the backend names the container",
    "--remove": "the backend removes the container itself",
    "--entrypoint": "the backend's keep-alive is the entrypoint",
    "--interactive": "the sandbox is started detached",
    "--tty": "the sandbox is started detached",
    "--version": "it would print the version instead of starting the sandbox",
    "--help": "it would print help instead of starting the sandbox",
}
_UNKNOWN = object()


@dataclass
class ExtraArgs:
    """Validated ``apple_container_extra_args``."""

    args: list[str] = field(default_factory=list)
    mounts: list[Mount] = field(default_factory=list)   # host folders and named volumes
    networks: list[str] = field(default_factory=list)   # `--network` values, in order
    host_access: bool = False   # a Mac folder, the SSH agent, or a flag we cannot read


def _walk(args: list[str], what: str) -> Iterator[tuple[str, Any]]:
    """(canonical flag, value) per flag; value is ``None`` for a switch and
    ``_UNKNOWN`` for a flag this table does not know."""
    index, count = 0, len(args)

    def value_after(flag: str) -> str:
        nonlocal index
        index += 1
        if index >= count:
            raise SpecError(f"{what}: {flag} needs a value")
        return args[index]

    while index < count:
        token = args[index]
        if token == "--":
            raise SpecError(f"{what}: `--` is not accepted (it would end the options the "
                            "backend adds after yours)")
        if token.startswith("--"):
            name, eq, inline = token.partition("=")
            name = _ALIASES.get(name, name)
            if name not in _RUN_FLAGS:
                # Unknown to this table (a newer CLI?): passed through; a
                # following bare word is taken to be its value.
                if not eq and index + 1 < count and not args[index + 1].startswith("-"):
                    index += 1
                yield name, _UNKNOWN
            elif _RUN_FLAGS[name]:
                yield name, inline if eq else value_after(name)
            elif eq:
                raise SpecError(f"{what}: {name} takes no value")
            else:
                yield name, None
        elif token.startswith("-") and len(token) > 1:
            body = token[1:]
            position = 0
            while position < len(body):  # combined short flags: `-iv SPEC`
                short = "-" + body[position]
                name = _ALIASES.get(short)
                if name is None:
                    yield short, _UNKNOWN
                    break
                if _RUN_FLAGS[name]:
                    rest = body[position + 1:]
                    if rest.startswith("="):
                        yield name, rest[1:]
                    elif rest:
                        yield name, rest
                    else:
                        yield name, value_after(short)
                    break
                yield name, None
                position += 1
        else:
            raise SpecError(f"{what}: {token!r} is not a flag; a bare word before the image "
                            "would be taken as the image itself")
        index += 1


def _parse_mount_flag(text: str, what: str) -> Optional[Mount]:
    """``--mount type=...,source=...,target=...[,readonly]``; ``None`` for tmpfs."""
    fields: dict[str, str] = {}
    readonly = False
    for part in text.split(","):
        key, eq, value = part.partition("=")
        key = key.strip()
        if key in ("readonly", "ro") and not eq:
            readonly = True
        elif eq and key in ("type", "source", "src", "target", "destination", "dst"):
            fields[{"src": "source", "destination": "target", "dst": "target"}.get(key, key)] = value
        else:
            raise SpecError(f"{what}: cannot read `{part}` in --mount {text!r}")
    kind = fields.get("type", "")
    if kind not in ("bind", "virtiofs", "volume", "tmpfs"):
        raise SpecError(f"{what}: --mount {text!r} needs type=bind, volume or tmpfs")
    label = f"{what} --mount {text!r}"
    if "target" not in fields:
        raise SpecError(f"{label}: target= is missing")
    target = _target(fields["target"], label)
    if kind == "tmpfs":
        return None
    source_kind, source = _source(fields.get("source", ""), label, expand_tilde=False)
    if (kind == "volume") != (source_kind == "volume"):
        raise SpecError(f"{label}: type={kind} does not match the source {source!r}")
    return Mount(source_kind, source, target, readonly)


def parse_extra_args(value: Any, *, what: str = "apple_container_extra_args") -> ExtraArgs:
    """Validate extra ``container run`` arguments, or :class:`SpecError`.

    Refused: anything but a list of strings (a dropped or split entry shifts
    the meaning of the others), control characters, flags the backend owns,
    labels in the backend's own namespace, bare words, and mounts that break
    the volume rules. Flags unknown to this table pass through, counted as
    host access."""
    if value is None:
        return ExtraArgs()
    if not isinstance(value, list):
        raise SpecError(f"{what} must be a list with one string per argument "
                        f"(e.g. [\"--network\", \"none\"]), got {value!r}")
    for entry in value:
        if not isinstance(entry, str):
            raise SpecError(f"{what}: every argument must be a string, got {entry!r} "
                            "(quote numbers)")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in entry):
            raise SpecError(f"{what}: control character in {entry!r}")
    result = ExtraArgs(args=list(value))
    for name, flag_value in _walk(result.args, what):
        if flag_value is _UNKNOWN:
            result.host_access = True
        elif name in _OWNED:
            raise SpecError(f"{what}: {name} is not accepted: {_OWNED[name]}")
        elif name == "--label" and flag_value.partition("=")[0].startswith("hermes-"):
            raise SpecError(f"{what}: the label {flag_value!r} is in the backend's own "
                            "`hermes-` namespace (it decides what the backend may delete)")
        elif name == "--volume":
            result.mounts.append(parse_volume(flag_value, what=f"{what} -v", expand_tilde=False))
        elif name == "--mount":
            mount = _parse_mount_flag(flag_value, what)
            if mount is not None:
                result.mounts.append(mount)
        elif name == "--network":
            result.networks.append(flag_value)
        elif name == "--ssh":
            result.host_access = True
    if any(mount.kind == "host" for mount in result.mounts):
        result.host_access = True
    return result


def extra_args_host_access(value: Any) -> bool:
    """True when the extra arguments give (or may give) the sandbox something
    of the Mac: a host mount in any spelling, the SSH agent, or anything that
    cannot be read. Flags that only tighten the sandbox answer False."""
    try:
        return parse_extra_args(value).host_access
    except SpecError:
        return True


# --- image references ---------------------------------------------------------
def normalize_image(reference: str) -> str:
    """The registry-qualified form ``container inspect`` reports: ``debian``
    is ``docker.io/library/debian:latest``."""
    name, at, digest = str(reference or "").strip().partition("@")
    tag = ""
    if ":" in name.rsplit("/", 1)[-1]:
        name, _, tag = name.rpartition(":")
    parts = name.split("/")
    first = parts[0]
    if not (len(parts) > 1 and ("." in first or ":" in first or first == "localhost")):
        name = "docker.io/" + (name if len(parts) > 1 else "library/" + name)
    if not tag and not at:
        tag = "latest"
    return name + (f":{tag}" if tag else "") + (f"@{digest}" if at else "")


def same_image(requested: str, reported: str) -> bool:
    """Whether the image a container runs is the one that was asked for."""
    return normalize_image(requested) == normalize_image(reported)
