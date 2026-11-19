"""Auto-configuration of template macros from what the agent found on a node (F8, phase 2).

The agent reports, every minute, the listening sockets, the running systemd services and the
well-known services it found with a few settings read from their configuration (see
`agent/internal/collect/services.go`: a fixed catalog, read only, no secret). A macro with an
`auto` rule takes its value from there; a person's own value always wins over it.

Pure functions only (no database): `resolve` a rule, `reasons` a template `detect` matches, and
`effective` the value and origin of each macro of a template on a node.
"""

import fnmatch
import re
from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

Origin = Literal["manual", "auto", "default"]


class Auto(BaseModel):
    """Where a macro value comes from on the node.

    - listening_port: the TCP port a process listens on (`process`, one name or several)
    - cmdline_arg:    the value of a flag (`arg`) on the command line of a catalog `service`
    - config:         a setting (`key`) the agent read from the configuration of a `service`
    - unit:           the first running systemd service matching a glob in `pattern`
    `pick` chooses among several values: the first, the lowest or the highest number.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: Literal["listening_port", "cmdline_arg", "config", "unit"] = Field(alias="from")
    process: str | list[str] | None = None
    service: str | None = Field(default=None, max_length=64)
    key: str | None = Field(default=None, max_length=64)
    arg: str | None = Field(default=None, max_length=64)
    pattern: str | list[str] | None = None
    pick: Literal["first", "min", "max"] = "first"

    @model_validator(mode="after")
    def _complete(self) -> "Auto":
        needs = {
            "listening_port": (self.process,),
            "cmdline_arg": (self.service, self.arg),
            "config": (self.service, self.key),
            "unit": (self.pattern,),
        }[self.source]
        if any(not n for n in needs):
            raise ValueError(f"auto rule {self.source} is incomplete")
        return self


class MacroLike(Protocol):
    name: str
    default: Any
    auto: Auto | None


def _names(value: str | list[str] | None) -> list[str]:
    return [value] if isinstance(value, str) else list(value or [])


def _pick(values: list[str], how: str) -> str | None:
    if not values:
        return None
    if how == "first":
        return values[0]
    numbers = [int(v) for v in values if re.fullmatch(r"\d+", v)]
    if not numbers:
        return values[0]
    return str(min(numbers) if how == "min" else max(numbers))


def _unit_names(facts: dict[str, Any]) -> list[str]:
    return [str(u) for u in facts.get("units") or []]


def resolve(auto: Auto, facts: dict[str, Any] | None) -> str | None:
    """The value the rule finds in the node's facts, or None when the node does not show it."""
    if not facts:
        return None
    if auto.source == "listening_port":
        wanted = _names(auto.process)
        ports = sorted(
            int(item["port"])
            for item in facts.get("listening") or []
            if item.get("proto") == "tcp" and item.get("process") in wanted
        )
        # `first` of a port list is the lowest one: listeners come sorted by port
        return _pick([str(p) for p in ports], "min" if auto.pick == "first" else auto.pick)
    if auto.source == "unit":
        patterns = _names(auto.pattern)
        found = []
        for unit in _unit_names(facts):
            bare = unit.removesuffix(".service")
            if any(fnmatch.fnmatchcase(x, p) for p in patterns for x in (unit, bare)):
                found.append(bare)
        return _pick(found, auto.pick)
    for service in facts.get("services") or []:
        if service.get("name") != auto.service:
            continue
        if auto.source == "config":
            value = (service.get("config") or {}).get(auto.key or "")
            return _pick([v for v in str(value).split(",") if v], auto.pick) if value else None
        tokens = str(service.get("cmd") or "").split()
        flag = auto.arg or ""
        for i, token in enumerate(tokens):
            if token == flag and i + 1 < len(tokens):
                return tokens[i + 1]
            if token.startswith(flag + "="):
                return token.split("=", 1)[1]
    return None


def reasons(detect: dict[str, Any], facts: dict[str, Any] | None) -> list[str]:
    """Why the template's `detect` rules match the node: empty when nothing does."""
    if not facts:
        return []
    found: list[str] = []
    listening = [item for item in facts.get("listening") or [] if item.get("proto") == "tcp"]
    process_names = {str(i.get("process")) for i in listening if i.get("process")} | {
        str(s.get("process")) for s in facts.get("services") or [] if s.get("process")
    }
    for pattern in detect.get("processes") or []:
        for name in sorted(process_names):
            if fnmatch.fnmatchcase(name, pattern):
                found.append(f"process {name}")
    listening_ports = {int(i["port"]) for i in listening}
    found += [f"port {p}" for p in detect.get("ports") or [] if int(p) in listening_ports]
    for pattern in detect.get("units") or []:
        for unit in _unit_names(facts):
            if fnmatch.fnmatchcase(unit, pattern) or fnmatch.fnmatchcase(
                unit.removesuffix(".service"), pattern
            ):
                found.append(f"unit {unit}")
    return list(dict.fromkeys(found))


def effective(
    macros: Sequence[MacroLike],
    stored: dict[str, Any],
    facts: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """The value and origin of each macro on a node.

    A value a person set (`manual`) is never replaced. Otherwise the auto rule's value from the
    facts; when the node does not show it any more, the last auto value that was stored; else the
    macro default."""
    out: dict[str, dict[str, Any]] = {}
    for macro in macros:
        own = stored.get(macro.name)
        if isinstance(own, dict) and own.get("origin") == "manual" and "value" in own:
            out[macro.name] = {"value": own["value"], "origin": "manual"}
            continue
        found = resolve(macro.auto, facts) if macro.auto else None
        if found is not None:
            out[macro.name] = {"value": found, "origin": "auto"}
        elif isinstance(own, dict) and own.get("origin") == "auto" and "value" in own:
            out[macro.name] = {"value": own["value"], "origin": "auto"}
        else:
            out[macro.name] = {"value": macro.default, "origin": "default"}
    return out
