# Standard
from unittest import mock
from unittest.mock import patch
import json
import os
import pathlib
import socket
import sys

# Third Party
from click.testing import CliRunner
from safetensors.torch import save_file
import pytest
import torch

# First Party
from instructlab import lab
from instructlab.model.backends import backends, common
from instructlab.model.backends.vllm import build_vllm_cmd, get_argument
from instructlab.utils import is_model_safetensors
from tests.test_feature_gates import dev_preview

test_json_config = {
    "model_type": "granite",
    "quantization_config": {"quant_method": "bitsandbytes"},
}

test_json_tokeninzer_config = {
    "bos_token": "<|beginning_of_text|>",
    "eos_token": "<|end_of_text|>",
    "chat_template": "test-chat-template",
}


# helper function to create dummy valid and invalid safetensor or bin model directories
def create_safetensors_or_bin_model_files(
    model_path: pathlib.Path,
    model_file_type: str,
    valid: bool,
    test_config=None,
    test_tokenizer_config=None,
):
    if test_config is None:
        test_config = test_json_config

    if test_tokenizer_config is None:
        test_tokenizer_config = test_json_tokeninzer_config

    cfg_object = json.dumps(test_config, indent=4)
    tcfg_object = json.dumps(test_tokenizer_config, indent=4)

    for file in ["tokenizer.json", "tokenizer_config.json"]:
        os.makedirs(os.path.dirname(model_path / file), exist_ok=True)
        with open(model_path / file, "a+", encoding="UTF-8") as f:
            f.write(tcfg_object)

    if model_file_type == "safetensors":
        tensors = {
            "tensor1": torch.randn(3, 3),
            "tensor2": torch.randn(5, 5),
        }
        save_file(tensors, model_path / f"model.{model_file_type}")
    else:
        with open(model_path / f"model.{model_file_type}", "a+", encoding="UTF-8"):
            pass
    if valid:
        with open(model_path / "config.json", "a+", encoding="UTF-8") as f:
            f.write(cfg_object)


def test_free_port():
    host = "localhost"
    port = common.free_tcp_ipv4_port(host)
    # check that port is bindable
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, port))


# the test fails because the model_path is not a valid GGUF file
def test_get_backend_auto_detection_fail_not_gguf(tmp_path: pathlib.Path):
    tmp_gguf = tmp_path / "test.gguf"
    # Write a known invalid header
    invalid_header = (
        b"\x00\x00\x00\x00"  # Use a header that will definitely not match "GGUF_MAGIC"
    )
    tmp_gguf.write_bytes(
        invalid_header + bytes([0] * 4093)
    )  # Fill the rest of the file with zeros
    with pytest.raises(ValueError) as exc_info:
        backends.get(tmp_gguf, None)
    assert "is not a GGUF format" in str(exc_info.value)


# this test succeeds because the model_path is a valid GGUF file (is_model_gguf mocked to returns True)
@patch("instructlab.model.backends.backends.is_model_gguf", return_value=True)
def test_get_backend_auto_detection_success_gguf(
    m_is_model_gguf, tmp_path: pathlib.Path
):
    tmp_gguf = tmp_path / "test.gguf"
    backend = backends.get(tmp_gguf, None)
    assert backend == "llama-cpp"
    m_is_model_gguf.assert_called_once_with(tmp_gguf)


# this tests both cases where a valid and invalid safetensors model directory is supplied
@pytest.mark.parametrize(
    "model_dir,model_file_type,expected",
    [
        (
            "valid_safetensors_model_dir",
            "safetensors",
            True,
        ),
        (
            "valid_bin_model_dir",
            "bin",
            True,
        ),
        (
            "invalid_bin_model_dir",
            "other",
            False,
        ),
        (
            "invalid_safetensors_model_dir",
            "safetensors",
            False,
        ),
    ],
)
def test_is_model_safetensors_or_bin_valid(
    model_dir: str, model_file_type: str, expected: bool, tmp_path: pathlib.Path
):
    model_path = tmp_path / model_dir
    create_safetensors_or_bin_model_files(model_path, model_file_type, expected)

    val = is_model_safetensors(model_path)
    assert val == expected


# this test succeeds because the model_path is a valid safetensors directory and the platform is linux so vllm is
# picked
@patch("sys.platform", "linux")
@pytest.mark.parametrize(
    "safetensors_dir,expected",
    [
        (
            "valid_safetensors_model_dir",
            "vllm",
        ),
    ],
)
def test_get_backend_auto_detection_success_vllm_dir(
    safetensors_dir: str, expected: str, tmp_path: pathlib.Path
):
    safetensors_model_path = tmp_path / safetensors_dir
    create_safetensors_or_bin_model_files(safetensors_model_path, "safetensors", True)

    backend = backends.get(safetensors_model_path, None)
    assert backend == expected


# this test fails because the OS is darwin and a directory is passed, only works on Linux
@patch("sys.platform", "darwin")
@pytest.mark.parametrize(
    "safetensors_dir",
    [
        ("valid_safetensors_model_dir"),
    ],
)
def test_get_backend_auto_detection_failed_vllm_dir_darwin(
    safetensors_dir: str, tmp_path: pathlib.Path
):
    with pytest.raises(ValueError) as exc_info:
        safetensors_model_path = tmp_path / safetensors_dir
        create_safetensors_or_bin_model_files(
            safetensors_model_path, "safetensors", True
        )
        backends.get(safetensors_model_path, None)
    assert "Cannot determine which backend to use" in str(exc_info.value)


# this test succeeds even if the auto-detection picked a different backend, we continue with what
# the user requested
@patch(
    "instructlab.model.backends.backends.determine_backend",
    return_value=("vllm", "reason for selection"),
)
def test_get_forced_backend_fails_autodetection(m_determine_backend):
    backend = backends.get("", "llama-cpp")
    assert backend == "llama-cpp"
    m_determine_backend.assert_called_once()


@mock.patch("instructlab.model.backends.vllm.Server")
@mock.patch("instructlab.model.backends.backends.get", return_value=backends.VLLM)
def test_ilab_vllm_args(
    m_backends_get: mock.Mock,
    m_server: mock.Mock,
    tmp_path: pathlib.Path,
    cli_runner: CliRunner,
):
    cmd = [
        "--config",
        "DEFAULT",
        "model",
        "serve",
        "--model-path",
        str(tmp_path),
        "--backend",
        backends.VLLM,
        "--",
        "--enable_lora",
    ]
    result = cli_runner.invoke(lab.ilab, cmd)
    assert result.exit_code == 0, result.stdout
    m_backends_get.assert_called_once_with(tmp_path, backends.VLLM)
    m_server.assert_called_once_with(
        api_base="http://127.0.0.1:8000/v1",
        chat_template=None,
        model_family=None,
        model_path=tmp_path,
        vllm_args=["--enable_lora"],
        host="127.0.0.1",
        port=8000,
        log_file=None,
    )


@pytest.mark.parametrize(
    "log_file_flag, expected_log_file",
    [
        (None, None),
        ("--log-file", pathlib.Path("test.log")),
    ],
)
@mock.patch("instructlab.model.backends.llama_cpp.Server")
@mock.patch("instructlab.model.backends.backends.get", return_value=backends.LLAMA_CPP)
def test_ilab_llama_cpp_args(
    m_backends_get: mock.Mock,
    m_server: mock.Mock,
    cli_runner: CliRunner,
    log_file_flag,
    expected_log_file,
):
    gguf = pathlib.Path("test.gguf")
    cmd = [
        "--config",
        "DEFAULT",
        "model",
        "serve",
        "--model-path",
        str(gguf),
        "--backend",
        backends.LLAMA_CPP,
    ]
    if log_file_flag:
        cmd.extend([log_file_flag, str(expected_log_file)])

    result = cli_runner.invoke(lab.ilab, cmd)
    assert result.exit_code == 0, result.stdout
    m_backends_get.assert_called_once_with(gguf, backends.LLAMA_CPP)
    m_server.assert_called_once_with(
        api_base="http://127.0.0.1:8000/v1",
        model_path=gguf,
        model_family=None,
        host="127.0.0.1",
        port=8000,
        gpu_layers=-1,
        max_ctx_size=4096,
        num_threads=None,
        chat_template=None,
        log_file=expected_log_file,
    )


def test_build_vllm_cmd_with_defaults(tmp_path: pathlib.Path):
    host = "localhost"
    port = 8080
    model_path = tmp_path / "model"
    model_family = ""
    chat_template = tmp_path / "chat_template.jinja2"
    chat_template.touch()
    vllm_args: list[str] = []
    expected_cmd = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--host",
        host,
        "--port",
        str(port),
        "--model",
        str(model_path),
        "--chat-template",
        str(chat_template),
        "--distributed-executor-backend",
        "mp",
        "--served-model-name",
        str(model_path),
        "model",
    ]
    cmd, _ = build_vllm_cmd(
        host, port, model_family, model_path, str(chat_template), vllm_args
    )
    assert cmd == expected_cmd


def test_build_vllm_cmd_with_args_provided(tmp_path: pathlib.Path):
    host = "localhost"
    port = 8080
    model_path = tmp_path / "model"
    model_family = ""
    chat_template = tmp_path / "chat_template.jinja2"
    chat_template.touch()
    vllm_args = [
        "--port",
        str(8001),
        "--model=/path/to/other/model",
        "--distributed-executor-backend",
        "ray",
    ]
    expected_cmd = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--host",
        host,
        "--chat-template",
        str(chat_template),
        "--served-model-name",
        str(model_path),
        "model",
    ] + vllm_args

    cmd, _ = build_vllm_cmd(
        host, port, model_family, model_path, str(chat_template), vllm_args
    )
    assert cmd == expected_cmd


def test_build_vllm_cmd_with_bnb_quant(tmp_path: pathlib.Path):
    host = "localhost"
    port = 8080
    model_path = tmp_path / "model"
    model_family = ""
    chat_template = tmp_path / "chat_template.jinja2"
    chat_template.touch()
    vllm_args: list[str] = []
    expected_cmd = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--host",
        host,
        "--port",
        str(port),
        "--model",
        str(model_path),
        "--chat-template",
        str(chat_template),
        "--quantization",
        "bitsandbytes",
        "--load-format",
        "bitsandbytes",
        "--enforce-eager",
        "--distributed-executor-backend",
        "mp",
        "--served-model-name",
        str(model_path),
        "model",
        "test_build_vllm_cmd_with_bnb_q0/model",
    ]
    create_safetensors_or_bin_model_files(model_path, "safetensors", True)
    cmd, _ = build_vllm_cmd(
        host, port, model_family, model_path, str(chat_template), vllm_args
    )
    assert cmd == expected_cmd


def test_get_model_template(tmp_path: pathlib.Path):
    # test successful read from tokenizer config
    test_tokeninzer_config = {
        "bos_token": "<|beginning_of_text|>",
        "eos_token": "<|end_of_text|>",
        "chat_template": "test-chat-template. add_generation_prompt",
    }

    model_path = tmp_path / "tmp_model"
    create_safetensors_or_bin_model_files(
        model_path,
        "safetensors",
        True,
        test_tokenizer_config=test_tokeninzer_config,
    )
    model_family = "granite"

    tmpl, eos, bos = common.get_model_template(model_family, model_path)
    assert (
        tmpl
        == '{% set eos_token = "<|end_of_text|>" %}\n{% set bos_token = "<|beginning_of_text|>" %}\ntest-chat-template. add_generation_prompt'
    )
    assert eos == "<|end_of_text|>"
    assert bos == "<|beginning_of_text|>"

    # test fallback to in-memory template
    test_tokeninzer_config = {
        "bos_token": "<|beginning_of_text|>",
        "eos_token": "<|end_of_text|>",
    }
    test_config = {
        "model_type": "llama",
        "quantization_config": {"quant_method": "bitsandbytes"},
    }

    model_path = tmp_path / "tmp_model"
    create_safetensors_or_bin_model_files(
        model_path,
        "safetensors",
        True,
        test_config=test_config,
        test_tokenizer_config=test_tokeninzer_config,
    )
    model_family = "granite"

    tmpl, eos, bos = common.get_model_template(model_family, model_path)
    assert (
        tmpl
        == "{% set eos_token = \"<|endoftext|>\" %}\n{% set bos_token = \"<|begginingoftext|>\" %}\n{% for message in messages %}{% if message['role'] == 'pretraining' %}{{'<|pretrain|>' + message['content'] + '<|endoftext|>' + '<|/pretrain|>' }}{% elif message['role'] == 'system' %}{{'<|system|>'+ '\n' + message['content'] + '\n'}}{% elif message['role'] == 'user' %}{{'<|user|>' + '\n' + message['content'] + '\n'}}{% elif message['role'] == 'assistant' %}{{'<|assistant|>' + '\n' + message['content'] + '<|endoftext|>' + ('' if loop.last else '\n')}}{% endif %}{% if loop.last and add_generation_prompt %}{{ '<|assistant|>' + '\n' }}{% endif %}{% endfor %}"
    )
    assert eos == "<|endoftext|>"
    assert bos == "<|begginingoftext|>"


@dev_preview
def test_model_exist_check(cli_runner: CliRunner):
    cmd = [
        "--config",
        "DEFAULT",
        "model",
        "chat",
        "-m",
        "test-model.gguf",
    ]
    result = cli_runner.invoke(lab.ilab, cmd)
    assert (
        "test-model.gguf does not exist. Please download model first." in result.stdout
    )


@pytest.mark.parametrize(
    "flag, args_list, expected_flag, expected_value",
    [
        ("--foo", ["--foo", "2"], True, "2"),
        ("--foo", ["--foo", "2", "--bar", "3"], True, "2"),
        ("--foo", ["--foo=2", "--bar", "3"], True, "2"),
        ("--foo", ["--test", "3", "--foo", "2", "--bar", "3"], True, "2"),
        ("--foo", ["--test", "3", "--foo=2", "--bar", "3"], True, "2"),
        ("--foo", ["--test", "3", "--foo", "2"], True, "2"),
        ("--foo", ["--test", "3", "--foo", "2", "--foo", "4"], True, "4"),
        ("--foo", ["--test", "3", "--foo", "2", "--foo=5"], True, "5"),
        ("--foo", ["--test", "3", "--foo=2"], True, "2"),
        ("--foo", ["--foo", "--foo"], True, None),
        ("--foo", ["--foo=2"], True, "2"),
        ("--foo", ["--foo"], True, None),
        ("--bar", ["--foo"], False, None),
        ("--foo", ["foo"], False, None),
        ("--foo", [], False, None),
    ],
)
def test_get_argument(flag, args_list, expected_flag, expected_value):
    result_flag, result_value = get_argument(flag, args_list)
    assert result_flag == expected_flag
    assert result_value == expected_value
