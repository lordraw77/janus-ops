"""A small reader of SNMP MIB files, to give names to OIDs (F8, FR-82).

It is NOT a MIB compiler. It reads the module text for the definitions that carry an OID:
`OBJECT IDENTIFIER`, `OBJECT-TYPE`, `NOTIFICATION-TYPE`, `MODULE-IDENTITY`, `OBJECT-IDENTITY`,
the groups and the SMIv1 `TRAP-TYPE`, resolves each `::= { parent n }` against the roots of the
tree, the names of the same file and the names already stored, and returns name, OID, kind and the
start of the description. Types, enumerations and ranges are ignored: the checks use numeric OIDs
and never depend on a MIB (see `SnmpParams`), the names are for people.

The file is only parsed, never executed or imported, and it is cut to MAX_BYTES.
"""

import re
from dataclasses import dataclass, field

MAX_BYTES = 2 * 1024 * 1024
MAX_OBJECTS = 20_000
DESCRIPTION_CHARS = 300

# the top of the tree and the groups every MIB hangs from
ROOTS: dict[str, str] = {
    "ccitt": "0",
    "iso": "1",
    "joint-iso-ccitt": "2",
    "org": "1.3",
    "dod": "1.3.6",
    "internet": "1.3.6.1",
    "directory": "1.3.6.1.1",
    "mgmt": "1.3.6.1.2",
    "mib-2": "1.3.6.1.2.1",
    "transmission": "1.3.6.1.2.1.10",
    "experimental": "1.3.6.1.3",
    "private": "1.3.6.1.4",
    "enterprises": "1.3.6.1.4.1",
    "security": "1.3.6.1.5",
    "snmpV2": "1.3.6.1.6",
    "snmpModules": "1.3.6.1.6.3",
}

# SNMPv2-MIB, IF-MIB and the system group: what a trap carries before any vendor MIB is loaded
BUILTIN: dict[str, str] = {
    "sysDescr": "1.3.6.1.2.1.1.1",
    "sysObjectID": "1.3.6.1.2.1.1.2",
    "sysUpTime": "1.3.6.1.2.1.1.3",
    "sysContact": "1.3.6.1.2.1.1.4",
    "sysName": "1.3.6.1.2.1.1.5",
    "sysLocation": "1.3.6.1.2.1.1.6",
    "ifIndex": "1.3.6.1.2.1.2.2.1.1",
    "ifDescr": "1.3.6.1.2.1.2.2.1.2",
    "ifType": "1.3.6.1.2.1.2.2.1.3",
    "ifAdminStatus": "1.3.6.1.2.1.2.2.1.7",
    "ifOperStatus": "1.3.6.1.2.1.2.2.1.8",
    "snmpTrapOID": "1.3.6.1.6.3.1.1.4.1",
    "snmpTrapEnterprise": "1.3.6.1.6.3.1.1.4.3",
    "coldStart": "1.3.6.1.6.3.1.1.5.1",
    "warmStart": "1.3.6.1.6.3.1.1.5.2",
    "linkDown": "1.3.6.1.6.3.1.1.5.3",
    "linkUp": "1.3.6.1.6.3.1.1.5.4",
    "authenticationFailure": "1.3.6.1.6.3.1.1.5.5",
    "egpNeighborLoss": "1.3.6.1.6.3.1.1.5.6",
}

MACROS = {
    "OBJECT-TYPE": "object",
    "NOTIFICATION-TYPE": "notification",
    "MODULE-IDENTITY": "identity",
    "OBJECT-IDENTITY": "identity",
    "OBJECT-GROUP": "group",
    "NOTIFICATION-GROUP": "group",
    "TRAP-TYPE": "notification",
}

_TOKEN = re.compile(
    r'"(?:[^"]|"")*"'  # a string
    r"|--[^\n]*"  # a comment
    r"|::="
    r"|[{}()]"
    r"|[^\s{}()\"]+"  # a word, a number, a name
)
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9-]*$")
_LABELLED = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)\((\d+)\)$")


class MibError(ValueError):
    """A file that cannot be read as a MIB; `code` is stable for the API."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MibObject:
    name: str
    oid: str
    kind: str  # object | notification | identity | group | node
    description: str = ""


@dataclass
class ParsedMib:
    module: str
    objects: list[MibObject] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def _tokens(text: str) -> list[str]:
    """The words of the file without comments and without the IMPORTS list, which names macros
    (`OBJECT-TYPE FROM SNMPv2-SMI`) that are not definitions."""
    out: list[str] = []
    importing = False
    for token in _TOKEN.findall(text):
        if token.startswith("--"):
            continue
        if token == "IMPORTS":  # noqa: S105 - a MIB keyword
            importing = True
        elif importing:
            importing = ";" not in token
        else:
            out.append(token)
    return out


def _clip(raw: str) -> str:
    body = raw[1:-1].replace('""', '"')
    return " ".join(body.split())[:DESCRIPTION_CHARS]


def _oid_value(tokens: list[str], start: int) -> tuple[list[str], int] | None:
    """The tokens inside `{ ... }` at `start`, and the index after the closing brace."""
    if start >= len(tokens) or tokens[start] != "{":
        return None
    parts: list[str] = []
    i = start + 1
    while i < len(tokens) and tokens[i] != "}":
        parts.append(tokens[i])
        i += 1
    return parts, i + 1


def _elements(parts: list[str]) -> list[str]:
    """`iso org ( 3 ) 1` -> `iso`, `org(3)`, `1`: the parentheses arrive as separate tokens."""
    out: list[str] = []
    i = 0
    while i < len(parts):
        if i + 3 < len(parts) and parts[i + 1] == "(" and parts[i + 3] == ")":
            out.append(f"{parts[i]}({parts[i + 2]})")
            i += 4
        else:
            out.append(parts[i])
            i += 1
    return out


def _resolve(elements: list[str], known: dict[str, str]) -> tuple[str | None, dict[str, str]]:
    """The numeric OID of a `{ parent 1 }` value, and the names it defined on the way
    (`org(3)` names `1.3`). None when the first element is not known yet."""
    defined: dict[str, str] = {}
    prefix: list[str] = []
    for n, element in enumerate(elements):
        labelled = _LABELLED.match(element)
        if element.isdigit():
            if n == 0:
                return None, {}
            prefix.append(element)
        elif labelled:
            prefix.append(labelled.group(2))
            defined[labelled.group(1)] = ".".join(prefix)
        elif n == 0 and element in known:
            prefix = known[element].split(".")
        else:
            return None, {}
    return ".".join(prefix), defined


def parse(text: str, known: dict[str, str] | None = None) -> ParsedMib:
    """The named OIDs of one module. `known` are names (name -> OID) from other modules."""
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        raise MibError("mib_too_large")
    tokens = _tokens(text)
    module = ""
    for i, tok in enumerate(tokens[:-2]):
        if tokens[i + 1] == "DEFINITIONS" and _NAME.match(tok):
            module = tok
            break
    if not module:
        raise MibError("not_a_mib")

    # (name, kind, elements, description) of what still needs a parent
    pending: list[tuple[str, str, list[str], str]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
        if tok == "OBJECT" and nxt == "IDENTIFIER" and i >= 1 and tokens[i + 2 : i + 3] == ["::="]:
            # `name OBJECT IDENTIFIER ::= { parent n }`; `SYNTAX OBJECT IDENTIFIER` has no `::=`
            value = _oid_value(tokens, i + 3)
            if value and _NAME.match(tokens[i - 1]):
                pending.append((tokens[i - 1], "node", _elements(value[0]), ""))
                i = value[1]
                continue
        if tok in MACROS and i >= 1 and _NAME.match(tokens[i - 1]) and tokens[i - 1] != "SYNTAX":
            name, kind = tokens[i - 1], MACROS[tok]
            description = ""
            enterprise = ""
            j = i + 1
            while j < len(tokens) and tokens[j] != "::=":
                if tokens[j] == "DESCRIPTION" and j + 1 < len(tokens):
                    description = _clip(tokens[j + 1]) if tokens[j + 1].startswith('"') else ""
                if tok == "TRAP-TYPE" and tokens[j] == "ENTERPRISE" and j + 1 < len(tokens):
                    enterprise = tokens[j + 1]
                j += 1
            value = _oid_value(tokens, j + 1) if j < len(tokens) else None
            if value:
                pending.append((name, kind, _elements(value[0]), description))
                i = value[1]
                continue
            if (
                tok == "TRAP-TYPE"
                and enterprise
                and j + 1 < len(tokens)
                and tokens[j + 1].isdigit()
            ):
                # SMIv1: the trap is `<enterprise>.0.<number>`
                pending.append((name, kind, [enterprise, "0", tokens[j + 1]], description))
                i = j + 2
                continue
        i += 1

    names = {**ROOTS, **BUILTIN, **(known or {})}
    result = ParsedMib(module)
    seen: set[str] = set()
    # a definition may use a parent that comes later in the file: go on while it resolves something
    while pending:
        still: list[tuple[str, str, list[str], str]] = []
        for name, kind, elements, description in pending:
            oid, defined = _resolve(elements, names)
            if oid is None:
                still.append((name, kind, elements, description))
                continue
            names.update(defined)
            names[name] = oid
            if name not in seen and len(result.objects) < MAX_OBJECTS:
                seen.add(name)
                result.objects.append(MibObject(name, oid, kind, description))
        if len(still) == len(pending):
            result.unresolved = sorted({p[0] for p in still})
            break
        pending = still
    return result


class NameIndex:
    """OID -> name, by the longest known prefix: `1.3.6.1.2.1.2.2.1.8.3` is `ifOperStatus.3`."""

    def __init__(self, extra: dict[str, str] | None = None) -> None:
        self._names: dict[str, str] = {}
        for name, oid in {**BUILTIN, **(extra or {})}.items():
            self._names.setdefault(oid, name)

    def lookup(self, oid: str) -> str | None:
        parts = oid.lstrip(".").split(".")
        for cut in range(len(parts), 0, -1):
            name = self._names.get(".".join(parts[:cut]))
            if name is not None:
                rest = ".".join(parts[cut:])
                return f"{name}.{rest}" if rest else name
        return None
