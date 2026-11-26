"""The MIB reader: names, parents defined later, SMIv1 traps, strings and comments."""

import pytest

from janus_ops.monitoring import mib

SYNO = """
SYNOLOGY-SYSTEM-MIB DEFINITIONS ::= BEGIN
IMPORTS enterprises FROM RFC1155-SMI
        OBJECT-TYPE FROM SNMPv2-SMI;   -- a comment with { fake 9 } inside

synology OBJECT IDENTIFIER ::= { enterprises 6574 }
systemStatus OBJECT IDENTIFIER ::= { synology 1 }

temperature OBJECT-TYPE
    SYNTAX      Integer32
    MAX-ACCESS  read-only
    STATUS      current
    DESCRIPTION "Temperature of the system
                 in -- degrees ""C""."
    ::= { systemStatus 2 }

diskTable OBJECT IDENTIFIER ::= { storageIO 2 }   -- parent comes later
storageIO OBJECT IDENTIFIER ::= { synology 8 }

diskFailed NOTIFICATION-TYPE
    OBJECTS { temperature }
    STATUS current
    DESCRIPTION "A disk failed"
    ::= { synology 20 }

oldTrap TRAP-TYPE
    ENTERPRISE synology
    DESCRIPTION "SMIv1 trap"
    ::= 7

holder OBJECT-TYPE
    SYNTAX OBJECT IDENTIFIER
    ::= { synology 99 }
orphan OBJECT IDENTIFIER ::= { nowhere 1 }
END
"""


def by_name(text: str) -> dict[str, mib.MibObject]:
    return {o.name: o for o in mib.parse(text).objects}


def test_names_oids_kinds_and_description() -> None:
    parsed = mib.parse(SYNO)
    assert parsed.module == "SYNOLOGY-SYSTEM-MIB"
    objs = {o.name: o for o in parsed.objects}
    assert objs["synology"].oid == "1.3.6.1.4.1.6574"
    assert objs["temperature"].oid == "1.3.6.1.4.1.6574.1.2"
    assert objs["temperature"].kind == "object"
    assert objs["temperature"].description == 'Temperature of the system in -- degrees "C".'
    assert objs["diskFailed"].kind == "notification"


def test_parent_defined_later_in_the_file() -> None:
    objs = by_name(SYNO)
    assert objs["diskTable"].oid == "1.3.6.1.4.1.6574.8.2"


def test_smiv1_trap_is_enterprise_zero_number() -> None:
    assert by_name(SYNO)["oldTrap"].oid == "1.3.6.1.4.1.6574.0.7"


def test_comment_and_syntax_object_identifier_are_not_definitions() -> None:
    parsed = mib.parse(SYNO)
    assert "fake" not in {o.name for o in parsed.objects}
    # `SYNTAX OBJECT IDENTIFIER` inside an OBJECT-TYPE is the type, the object is still `holder`
    assert {o.name: o.oid for o in parsed.objects}["holder"] == "1.3.6.1.4.1.6574.99"
    assert parsed.unresolved == ["orphan"]


def test_inline_numbers_and_known_names_from_other_modules() -> None:
    text = (
        "M DEFINITIONS ::= BEGIN\nvendor OBJECT IDENTIFIER ::= { iso org(3) dod(6) 1 4 1 4242 }\n"
        "leaf OBJECT IDENTIFIER ::= { other 5 }\nEND"
    )
    objs = {o.name: o.oid for o in mib.parse(text, {"other": "1.3.6.1.4.1.1"}).objects}
    assert objs["vendor"] == "1.3.6.1.4.1.4242"
    assert objs["leaf"] == "1.3.6.1.4.1.1.5"


def test_not_a_mib_and_too_large() -> None:
    with pytest.raises(mib.MibError) as exc:
        mib.parse("hello world")
    assert exc.value.code == "not_a_mib"
    with pytest.raises(mib.MibError) as big:
        mib.parse("x" * (mib.MAX_BYTES + 1))
    assert big.value.code == "mib_too_large"


def test_name_index_uses_the_longest_prefix_and_keeps_the_instance() -> None:
    index = mib.NameIndex(
        {"temperature": "1.3.6.1.4.1.6574.1.2", "systemStatus": "1.3.6.1.4.1.6574.1"}
    )
    assert index.lookup("1.3.6.1.4.1.6574.1.2.0") == "temperature.0"
    assert index.lookup("1.3.6.1.2.1.2.2.1.8.3") == "ifOperStatus.3"
    assert index.lookup("1.3.6.1.6.3.1.1.5.3") == "linkDown"
    assert index.lookup("9.9.9") is None
