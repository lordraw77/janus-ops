from janus_ops.ai.mcp import runner
from janus_ops.ai.mcp.spec import KEY_DIR, McpSpec


def test_argv_has_variable_names_never_values() -> None:
    spec = McpSpec(
        "img:1",
        {"UXMCP_SERVER_1_PASSWORD": "hunter2", "UXMCP_TRANSPORT": "stdio"},
        {f"{KEY_DIR}/key_1": "PRIVATE-KEY-BODY"},
    )
    args = runner.docker_args(spec, ["python"], ["-m", "srv"], "n")
    joined = " ".join(args)
    assert "hunter2" not in joined and "PRIVATE-KEY-BODY" not in joined
    assert "UXMCP_SERVER_1_PASSWORD" in args and "JANUS_FILE_0" in args
    assert "--cap-drop=ALL" in args and "no-new-privileges" in args
    assert args[args.index("--entrypoint") + 1] == "sh"
    assert args[args.index("sh", args.index("-c")) + 1 :] == ["python", "-m", "srv"]


def test_without_files_the_image_runs_as_is() -> None:
    args = runner.docker_args(McpSpec("img:1", {"A": "1"}, {}), ["x"], [], "n")
    assert args[-1] == "img:1" and "--entrypoint" not in args


def test_script_writes_files_then_forgets_the_variables() -> None:
    script = runner.build_script([f"{KEY_DIR}/key_1", f"{KEY_DIR}/key_2"])
    assert script.index("JANUS_FILE_0") < script.index("unset JANUS_FILE_0")
    assert script.endswith('exec "$@"') and "umask 077" in script


def test_process_env_carries_values_only_for_the_client() -> None:
    spec = McpSpec("i", {"A": "1"}, {"/k": "body"})
    env = runner.process_env(spec, "unix:///x.sock")
    assert env["A"] == "1" and env["JANUS_FILE_0"] == "body" and env["DOCKER_HOST"]
