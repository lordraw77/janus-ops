"""Surveys (F6): questions asked before a run, declared next to the playbook.

A playbook `site.yml` has its survey in `site.survey.json` in the same source:

    {"questions": [{"name": "app_version", "label": "Version", "type": "text",
                    "required": true, "default": "1.4"}]}

Types: text, integer, boolean, choice. There is no password type: answers are plain values, they
are passed to ansible as extra vars from a 0600 file and kept with the run in the history.
"""

import json
import re
from typing import Any

from janus_ops.ansible.catalog import CatalogError

SURVEY_SUFFIX = ".survey.json"
QUESTION_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
INTEGER = re.compile(r"^-?\d{1,18}$")
TYPES = ("text", "integer", "boolean", "choice")
MAX_QUESTIONS = 50
MAX_CHOICES = 50
MAX_TEXT = 1000
MAX_LABEL = 200


def survey_path(playbook: str) -> str:
    """`ops/site.yml` -> `ops/site.survey.json`."""
    return re.sub(r"\.ya?ml$", "", playbook, flags=re.IGNORECASE) + SURVEY_SUFFIX


def parse_survey(raw: str) -> list[dict[str, Any]]:
    """The questions of a survey file, validated. Raises CatalogError('survey_invalid')."""
    try:
        data = json.loads(raw)
    except ValueError:
        raise CatalogError("survey_invalid") from None
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list) or len(questions) > MAX_QUESTIONS:
        raise CatalogError("survey_invalid")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for raw_question in questions:
        question = _parse_question(raw_question)
        if question["name"] in seen:
            raise CatalogError("survey_invalid")
        seen.add(question["name"])
        result.append(question)
    return result


def _parse_question(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise CatalogError("survey_invalid")
    name = raw.get("name")
    qtype = raw.get("type", "text")
    label = raw.get("label", name)
    if not isinstance(name, str) or not QUESTION_NAME.match(name):
        raise CatalogError("survey_invalid")
    if qtype not in TYPES or not isinstance(label, str) or len(label) > MAX_LABEL:
        raise CatalogError("survey_invalid")
    question: dict[str, Any] = {
        "name": name,
        "label": label,
        "type": qtype,
        "required": raw.get("required", False) is True,
    }
    if qtype == "choice":
        choices = raw.get("choices")
        if (
            not isinstance(choices, list)
            or not 1 <= len(choices) <= MAX_CHOICES
            or not all(isinstance(c, str) and c for c in choices)
        ):
            raise CatalogError("survey_invalid")
        question["choices"] = choices
    if raw.get("default") is not None:
        question["default"] = coerce(question, raw["default"])
    return question


def coerce(question: dict[str, Any], value: Any) -> Any:
    """The value as the question's type. Raises CatalogError('survey_value_invalid')."""
    qtype = question["type"]
    if qtype == "text":
        if isinstance(value, str) and len(value) <= MAX_TEXT:
            return value
    elif qtype == "integer":
        if isinstance(value, bool):
            pass
        elif isinstance(value, int):
            return value
        elif isinstance(value, str) and INTEGER.match(value):
            return int(value)
    elif qtype == "boolean":
        if isinstance(value, bool):
            return value
        if value in ("true", "false"):
            return value == "true"
    elif qtype == "choice" and isinstance(value, str) and value in question["choices"]:
        return value
    raise CatalogError("survey_value_invalid")


def resolve(questions: list[dict[str, Any]], answers: dict[str, Any]) -> dict[str, Any]:
    """The values a run uses: the answers, then the defaults. Missing required answers refused."""
    known = {q["name"] for q in questions}
    if any(name not in known for name in answers):
        raise CatalogError("survey_unknown_question")
    values: dict[str, Any] = {}
    for question in questions:
        name = question["name"]
        given = answers.get(name)
        if given is not None and given != "":
            values[name] = coerce(question, given)
        elif "default" in question:
            values[name] = question["default"]
        elif question["required"]:
            raise CatalogError("survey_required")
    return values
