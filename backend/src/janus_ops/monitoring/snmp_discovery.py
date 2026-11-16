"""Turns the rows of an SNMP walk into catalog candidates (F8, FR-82).

Conventions used: a scalar object ends in `.0` and is its own catalog entry. Any other OID is a
table cell: its last number is the instance (disk, pool, volume) and the rest is the column. So
`1.3.6.1.4.1.6574.2.1.1.3.1` (disk 1 model) becomes the column `1.3.6.1.4.1.6574.2.1.1.3`, indexed,
with instance 1. This is a heuristic: the MIB guide and the device decide what is right, and the
user confirms each entry before it reaches the catalog.
"""

from dataclasses import dataclass, field

from janus_ops.monitoring.snmp_poll import Row

SAMPLE_MAX = 120


@dataclass
class Candidate:
    oid: str  # catalog OID: the scalar itself, or the table column
    indexed: bool
    instances: list[int] = field(default_factory=list)
    sample: str = ""
    number: float | None = None
    example: str = ""  # one full OID that answered, for the user to check


def _split(oid: str) -> tuple[str, int | None]:
    """(catalog OID, instance) for one answered OID."""
    arcs = oid.split(".")
    if len(arcs) >= 2 and arcs[-1] == "0":
        return oid, None  # a scalar
    if len(arcs) < 3 or not arcs[-1].isdigit():
        return oid, None
    return ".".join(arcs[:-1]), int(arcs[-1])


def candidates(rows: list[Row]) -> list[Candidate]:
    """Groups rows by catalog OID. Keeps the first sample and the instances seen, in walk order."""
    by_oid: dict[str, Candidate] = {}
    for row in rows:
        oid = row.oid.lstrip(".")
        column, instance = _split(oid)
        cand = by_oid.get(column)
        if cand is None:
            cand = Candidate(
                oid=column,
                indexed=instance is not None,
                sample=row.text[:SAMPLE_MAX],
                number=row.number,
                example=oid,
            )
            by_oid[column] = cand
        if instance is not None and instance not in cand.instances:
            cand.instances.append(instance)
    return list(by_oid.values())
