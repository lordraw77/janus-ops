"""GitOps (F8): input validation, the command plan of a deploy and the webhook signature. No
database and no agent: the deploy itself is tested in test_gitops_integration.py."""

import hashlib
import hmac
import uuid

import pytest

from janus_ops.gitops import deploy
from janus_ops.gitops.deploy import GitopsError, Stack
from janus_ops.gitops.router import _verify

STACK_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
DIR = f"{deploy.GITOPS_ROOT}/{STACK_ID}"


def _stack(**overrides: str) -> Stack:
    values = {
        "id": STACK_ID,
        "name": "web",
        "node_id": str(uuid.uuid4()),
        "repo_url": "https://git.example.com/org/web.git",
        "branch": "main",
        "compose_file": "docker-compose.yml",
    }
    values.update(overrides)
    return Stack(**values)


@pytest.mark.parametrize("value", ["web", "prod-web_1", "a", "0-app"])
def test_valid_names(value: str) -> None:
    assert deploy.validate_name(value) == value


@pytest.mark.parametrize("value", ["", "Web", "-web", "_web", "web app", "a" * 49, "web;rm"])
def test_invalid_names(value: str) -> None:
    with pytest.raises(GitopsError) as exc:
        deploy.validate_name(value)
    assert exc.value.code == "invalid_name"


@pytest.mark.parametrize(
    "value",
    [
        "https://git.example.com/org/web.git",
        "https://git.example.com:8443/org/web",
    ],
)
def test_valid_repo_urls(value: str) -> None:
    assert deploy.validate_repo_url(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "http://git.example.com/org/web.git",  # not https
        "ssh://git@git.example.com/org/web.git",
        "git@git.example.com:org/web.git",
        "https://user:secret@git.example.com/org/web.git",  # credentials stay out of the URL
        "https://git.example.com/org/web.git?ref=x",
        "https://git.example.com/org/web.git#x",
        "https:///org/web.git",
        "https://git.example.com",
        "https://git.example.com/",
        "https://git.example.com/org/ web.git",
        "https://git.example.com/" + "a" * 2100,
    ],
)
def test_invalid_repo_urls(value: str) -> None:
    with pytest.raises(GitopsError) as exc:
        deploy.validate_repo_url(value)
    assert exc.value.code == "invalid_repo_url"


@pytest.mark.parametrize("value", ["main", "release/1.2", "feature_x-2"])
def test_valid_branches(value: str) -> None:
    assert deploy.validate_branch(value) == value


@pytest.mark.parametrize("value", ["", "-x", "--upload-pack=x", "a..b", "a/", "x.", "a b", "a;b"])
def test_invalid_branches(value: str) -> None:
    with pytest.raises(GitopsError) as exc:
        deploy.validate_branch(value)
    assert exc.value.code == "invalid_branch"


@pytest.mark.parametrize(
    "value", ["docker-compose.yml", "stacks/app/compose.yaml", ".compose.yml", "a_b/c-d.yml"]
)
def test_valid_compose_files(value: str) -> None:
    assert deploy.validate_compose_file(value) == value


@pytest.mark.parametrize(
    "value",
    ["/etc/compose.yml", "../compose.yml", "app/../compose.yml", "app//x.yml", "-x.yml", "a b.yml"],
)
def test_invalid_compose_files(value: str) -> None:
    with pytest.raises(GitopsError) as exc:
        deploy.validate_compose_file(value)
    assert exc.value.code == "invalid_compose_file"


def test_checkout_dir_is_built_from_a_uuid_only() -> None:
    assert deploy.checkout_dir(STACK_ID) == DIR
    with pytest.raises(ValueError):
        deploy.checkout_dir("../../etc")


def test_first_deploy_clones_then_runs_compose() -> None:
    steps = deploy.plan(_stack(), checkout_exists=False)
    labels = [s.label for s in steps]
    assert labels == ["mkdir", "git clone", "git commit", "compose up"]
    clone = steps[1].argv
    # the URL follows `--`, so it cannot be read as an option
    assert clone[clone.index("--") + 1] == "https://git.example.com/org/web.git"
    assert clone[clone.index("--") + 2] == DIR
    assert "GIT_TERMINAL_PROMPT=0" in clone


def test_later_deploys_fetch_and_reset_the_existing_checkout() -> None:
    steps = deploy.plan(_stack(), checkout_exists=True)
    labels = [s.label for s in steps]
    assert labels == [
        "mkdir",
        "git remote",
        "git fetch",
        "git reset",
        "git commit",
        "compose up",
    ]
    fetch = steps[2].argv
    assert fetch[-2:] == ["origin", "main"]
    assert steps[3].argv[-1] == "FETCH_HEAD"


def test_compose_runs_on_the_checkout_with_the_stack_name_as_project() -> None:
    steps = deploy.plan(_stack(name="shop", compose_file="deploy/app.yml"), checkout_exists=True)
    compose = steps[-1]
    assert compose.argv == [
        "docker",
        "compose",
        "-p",
        "shop",
        "-f",
        f"{DIR}/deploy/app.yml",
        "up",
        "-d",
        "--remove-orphans",
    ]
    assert compose.timeout_s == deploy.COMPOSE_TIMEOUT_S


def test_every_step_is_argv_without_a_shell() -> None:
    for exists in (True, False):
        for step in deploy.plan(_stack(), checkout_exists=exists):
            assert step.argv and all(isinstance(a, str) and a for a in step.argv)
            assert step.argv[0] not in ("sh", "bash", "/bin/sh", "/bin/bash")
            assert step.timeout_s <= 300


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_signature_is_checked_over_the_raw_body() -> None:
    body = b'{"ref": "refs/heads/main"}'
    assert _verify("s3cret", body, _sign("s3cret", body))
    assert not _verify("s3cret", body + b" ", _sign("s3cret", body))
    assert not _verify("other", body, _sign("s3cret", body))


def test_webhook_without_a_signature_is_refused() -> None:
    assert not _verify("s3cret", b"{}", "")
    assert not _verify("s3cret", b"{}", "sha256=")
