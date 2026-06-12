"""Runner entrypoint argument-parsing smoke tests (backend-architecture §8.1)."""

import pytest

from runner.__main__ import ROLES, parse_args, service_name


def test_default_role_is_all() -> None:
    args = parse_args([])
    assert args.role == "all"


@pytest.mark.parametrize("role", ROLES)
def test_explicit_roles_parse(role: str) -> None:
    assert parse_args(["--role", role]).role == role


def test_unknown_role_exits() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--role", "websockets"])


def test_service_name_maps_sinks_to_buffer_writer() -> None:
    """Dev `buffer-writer` container runs `--role sinks` (deployment §2.1)."""
    assert service_name("generation") == "runner"
    assert service_name("all") == "runner"
    assert service_name("sinks") == "buffer-writer"
