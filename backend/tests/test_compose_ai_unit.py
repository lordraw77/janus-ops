"""Compose generation with a model: secrets are masked before the file leaves, and put back."""

import pytest

from janus_ops.ai.llm import AIError
from janus_ops.containers import compose_ai

CURRENT = """services:
  db:
    image: postgres:17
    environment:
      POSTGRES_PASSWORD: hunter2
      POSTGRES_USER: app
      API_TOKEN: "abc123def"
      FROM_ENV: ${SECRET_FROM_ENV}
  web:
    image: nginx
    environment:
      - DB_PASSWORD=s3cret-value
      - MODE=prod
"""


def test_literal_secrets_are_masked_and_variables_are_left() -> None:
    masked, back = compose_ai.mask(CURRENT)
    for leaked in ("hunter2", "abc123def", "s3cret-value"):
        assert leaked not in masked
    assert "POSTGRES_USER: app" in masked and "MODE=prod" in masked
    assert "${SECRET_FROM_ENV}" in masked  # a reference, not a secret
    assert sorted(back.values()) == ["abc123def", "hunter2", "s3cret-value"]
    assert compose_ai.unmask(masked, back) == CURRENT


def test_messages_never_carry_the_secret() -> None:
    messages, back = compose_ai.build_messages(
        "dock-01", "add redis", CURRENT, ["8080/tcp"], ["shop"]
    )
    text = "\n".join(m["content"] for m in messages)
    assert "hunter2" not in text and "s3cret-value" not in text
    assert "8080/tcp" in text and "shop" in text and len(back) == 3
    new_messages, new_back = compose_ai.build_messages("dock-01", "a wiki", None, [], [])
    assert not new_back and "new Compose file" in new_messages[1]["content"]


def test_answer_gets_secrets_back_and_warnings() -> None:
    masked, back = compose_ai.mask(CURRENT)
    answer = f"```yaml\n{masked}  cache:\n    image: redis:7\n```\n- added redis\n"
    code, notes, warnings = compose_ai.read_answer(answer, back)
    assert "hunter2" in code and "__JANUS_SECRET" not in code
    assert notes == ["added redis"]
    assert warnings == []  # a secret that was already in the file is not a finding


def test_invented_placeholder_is_refused() -> None:
    with pytest.raises(AIError):
        compose_ai.read_answer(
            "```yaml\nservices:\n  a:\n    image: x\n    environment:\n"
            "      K: __JANUS_SECRET_9__\n```\n",
            {},
        )


def test_answer_without_a_file_is_refused() -> None:
    with pytest.raises(AIError):
        compose_ai.read_answer("sorry, I cannot", {})
