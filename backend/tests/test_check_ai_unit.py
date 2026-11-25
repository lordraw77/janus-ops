"""Drafting checks with a model: only a JSON block in the answer is read, and each draft goes
through the same validation as a check created by hand."""

import pytest

from janus_ops.ai.llm import AIError
from janus_ops.monitoring import check_ai
from janus_ops.monitoring.router import CheckIn

ANSWER = """```json
{"checks": [
  {"name": "nginx running", "kind": "service", "params": {"unit": "nginx.service"}},
  {"name": "nginx log errors", "kind": "logs",
   "params": {"source": "file", "path": "/var/log/nginx/error.log"}, "check_cycles": 3},
  "not a check"
]}
```
- **Assumed** the unit is `nginx.service`
* the log path is the Debian default
"""


def test_answer_is_read_and_notes_are_cleaned() -> None:
    items, notes = check_ai.read_answer(ANSWER)
    assert [i["kind"] for i in items] == ["service", "logs"]  # the string is dropped
    assert notes == ["Assumed the unit is nginx.service", "the log path is the Debian default"]


def test_drafts_validate_like_manual_checks() -> None:
    items, _ = check_ai.read_answer(ANSWER)
    drafts = [CheckIn.model_validate(i) for i in items]
    assert drafts[1].params["path"] == "/var/log/nginx/error.log"
    assert drafts[1].params["words"] == ["error", "fatal", "exception", "panic"]  # defaults filled
    with pytest.raises(ValueError):  # a path outside /var/log is refused, whatever the model says
        CheckIn.model_validate(
            {"name": "x", "kind": "logs", "params": {"source": "file", "path": "/etc/shadow"}}
        )


@pytest.mark.parametrize(
    "bad", ["no block here", "```json\n{not json}\n```", '```json\n{"a": 1}\n```']
)
def test_unusable_answers_are_refused(bad: str) -> None:
    with pytest.raises(AIError):
        check_ai.read_answer(bad)


def test_kinds_a_model_may_not_draft() -> None:
    assert not {"backup", "snmp", "fritzbox"} & check_ai.KINDS
