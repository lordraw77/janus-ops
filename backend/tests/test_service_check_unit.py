"""systemd service checks (`service` kind): how `systemctl is-active` output is graded.
No network."""

import pytest
from pydantic import ValidationError

from janus_ops.monitoring.checks import ServiceParams, grade_service


@pytest.mark.parametrize(
    ("output", "state"),
    [
        ("active\n", "ok"),
        ("activating\n", "warn"),
        ("reloading\n", "warn"),
        ("inactive\n", "crit"),
        ("failed\n", "crit"),
        ("", "crit"),
    ],
)
def test_is_active_output_is_graded(output: str, state: str) -> None:
    assert grade_service("ser2net.service", output).state == state


def test_the_output_names_the_unit_and_its_state() -> None:
    assert grade_service("ser2net.service", "inactive\n").output == "ser2net.service is inactive"


@pytest.mark.parametrize(
    "unit", ["ser2net.service", "ser2net", "getty@tty1.service", "a-b_c.timer"]
)
def test_valid_unit_names(unit: str) -> None:
    assert ServiceParams.model_validate({"unit": unit}).unit == unit


@pytest.mark.parametrize("unit", ["", "ser2net; rm -rf /", "a b", "x$(id)", "../etc"])
def test_unit_names_cannot_carry_a_command(unit: str) -> None:
    with pytest.raises(ValidationError):
        ServiceParams.model_validate({"unit": unit})
