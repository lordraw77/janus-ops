"""SNMP discovery (FR-82): turning walk rows into catalog candidates. No network."""

import pytest

from janus_ops.monitoring.snmp_discovery import SAMPLE_MAX, _split, candidates
from janus_ops.monitoring.snmp_poll import Row


def _row(oid: str, text: str = "x", number: float | None = None) -> Row:
    return Row(oid, text, number)


@pytest.mark.parametrize(
    ("oid", "column", "instance"),
    [
        ("1.3.6.1.4.1.6574.1.2.0", "1.3.6.1.4.1.6574.1.2.0", None),  # a scalar
        ("1.3.6.1.4.1.6574.2.1.1.3.1", "1.3.6.1.4.1.6574.2.1.1.3", 1),  # disk 1, model
        ("1.3.6.1.4.1.6574.3.1.1.4.12", "1.3.6.1.4.1.6574.3.1.1.4", 12),  # pool 12
    ],
)
def test_split_separates_the_column_from_the_instance(
    oid: str, column: str, instance: int | None
) -> None:
    assert _split(oid) == (column, instance)


def test_table_cells_become_one_indexed_column_with_its_instances() -> None:
    rows = [
        _row("1.3.6.1.4.1.6574.2.1.1.3.1", "WD Red 4TB", None),
        _row("1.3.6.1.4.1.6574.2.1.1.3.2", "WD Red 4TB", None),
        _row("1.3.6.1.4.1.6574.2.1.1.3.3", "ST8000", None),
    ]
    (cand,) = candidates(rows)
    assert cand.oid == "1.3.6.1.4.1.6574.2.1.1.3"
    assert cand.indexed is True
    assert cand.instances == [1, 2, 3]
    assert cand.sample == "WD Red 4TB"  # the first answer is the sample
    assert cand.example == "1.3.6.1.4.1.6574.2.1.1.3.1"


def test_scalars_are_their_own_entries_and_not_indexed() -> None:
    rows = [_row("1.3.6.1.4.1.6574.1.2.0", "40", 40.0), _row("1.3.6.1.2.1.1.5.0", "ds920")]
    by_oid = {c.oid: c for c in candidates(rows)}
    assert by_oid["1.3.6.1.4.1.6574.1.2.0"].indexed is False
    assert by_oid["1.3.6.1.4.1.6574.1.2.0"].number == 40.0
    assert by_oid["1.3.6.1.2.1.1.5.0"].sample == "ds920"


def test_instances_are_not_repeated_and_samples_are_clipped() -> None:
    rows = [
        _row("1.3.6.1.4.1.6574.2.1.1.6.1", "y" * 500),
        _row("1.3.6.1.4.1.6574.2.1.1.6.1", "y" * 500),
    ]
    (cand,) = candidates(rows)
    assert cand.instances == [1]
    assert len(cand.sample) == SAMPLE_MAX


def test_an_oid_without_a_number_at_the_end_is_kept_as_it_is() -> None:
    (cand,) = candidates([_row("1.3.6.1.2.1.1.x", "odd")])
    assert cand.oid == "1.3.6.1.2.1.1.x" and cand.indexed is False
