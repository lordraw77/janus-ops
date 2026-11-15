"""Surveys of playbooks (F6): parsing, answers, defaults and the file names."""

import pytest

from janus_ops.ansible.catalog import CatalogError
from janus_ops.ansible.survey import coerce, parse_survey, resolve, survey_path


def _code(exc: pytest.ExceptionInfo[CatalogError]) -> str:
    return exc.value.code


def test_survey_sits_next_to_the_playbook() -> None:
    assert survey_path("site.yml") == "site.survey.json"
    assert survey_path("ops/deploy.YAML") == "ops/deploy.survey.json"


SPEC = """{"questions": [
  {"name": "app_version", "label": "Version", "type": "text", "required": true},
  {"name": "replicas", "type": "integer", "default": 2},
  {"name": "drain", "type": "boolean", "default": "false"},
  {"name": "channel", "type": "choice", "choices": ["stable", "beta"], "default": "stable"}
]}"""


def test_parse_keeps_questions_and_coerces_defaults() -> None:
    questions = parse_survey(SPEC)
    assert [q["name"] for q in questions] == ["app_version", "replicas", "drain", "channel"]
    assert questions[0]["label"] == "Version"
    assert questions[0]["required"] is True
    assert questions[1]["default"] == 2
    assert questions[2]["default"] is False
    assert questions[3]["choices"] == ["stable", "beta"]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("not json", "survey_invalid"),
        ('{"questions": {}}', "survey_invalid"),
        ('{"questions": [{"name": "Bad Name"}]}', "survey_invalid"),
        ('{"questions": [{"name": "a"}, {"name": "a"}]}', "survey_invalid"),
        ('{"questions": [{"name": "a", "type": "password"}]}', "survey_invalid"),
        ('{"questions": [{"name": "a", "type": "choice"}]}', "survey_invalid"),
        (
            '{"questions": [{"name": "a", "type": "integer", "default": "x"}]}',
            "survey_value_invalid",
        ),
        ('{"questions": [{"name": "a", "type": "boolean", "default": 1}]}', "survey_value_invalid"),
    ],
)
def test_invalid_survey_is_refused(raw: str, code: str) -> None:
    with pytest.raises(CatalogError) as exc:
        parse_survey(raw)
    assert _code(exc) == code


def test_answers_override_defaults_and_are_typed() -> None:
    questions = parse_survey(SPEC)
    values = resolve(
        questions, {"app_version": "1.4", "replicas": "5", "drain": True, "channel": "beta"}
    )
    assert values == {"app_version": "1.4", "replicas": 5, "drain": True, "channel": "beta"}


def test_defaults_fill_the_gaps() -> None:
    values = resolve(parse_survey(SPEC), {"app_version": "1.4"})
    assert values == {"app_version": "1.4", "replicas": 2, "drain": False, "channel": "stable"}


def test_missing_required_answer_is_refused() -> None:
    with pytest.raises(CatalogError) as exc:
        resolve(parse_survey(SPEC), {"app_version": ""})
    assert _code(exc) == "survey_required"


def test_unknown_question_and_wrong_value_are_refused() -> None:
    questions = parse_survey(SPEC)
    with pytest.raises(CatalogError) as exc:
        resolve(questions, {"app_version": "1", "nope": "x"})
    assert _code(exc) == "survey_unknown_question"
    with pytest.raises(CatalogError) as exc:
        resolve(questions, {"app_version": "1", "channel": "nightly"})
    assert _code(exc) == "survey_value_invalid"


def test_integer_refuses_booleans_and_text() -> None:
    question = {"name": "n", "type": "integer", "required": False}
    assert coerce(question, -3) == -3
    for bad in (True, "3.5", "abc"):
        with pytest.raises(CatalogError):
            coerce(question, bad)


def test_text_is_bounded() -> None:
    question = {"name": "t", "type": "text", "required": False}
    with pytest.raises(CatalogError):
        coerce(question, "x" * 1001)
