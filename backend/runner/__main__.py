"""Data-plane entrypoint: `python -m runner [--role generation|sinks|all]`
(backend-architecture §8.1).

Boot sequence: `django.setup()` (the runner is a Django-context process), then
the supervisor: an asyncio program running the heartbeat loop and the internal
aiohttp health listener on :8081.
"""

import argparse
import asyncio
import os
from collections.abc import Sequence

ROLES = ("generation", "sinks", "all")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m runner",
        description="DataForge data-plane process (backend-architecture §8).",
    )
    parser.add_argument(
        "--role",
        choices=ROLES,
        default="all",
        help="generation = shard runners; sinks = delivery sink consumers; "
        "all = both (default).",
    )
    return parser.parse_args(argv)


def service_name(role: str) -> str:
    """Log `service` label per observability §2.2 (dev `buffer-writer` container
    runs `--role sinks`, deployment-architecture §2.1)."""
    return "buffer-writer" if role == "sinks" else "runner"


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ.setdefault("DF_SERVICE", service_name(args.role))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

    import django

    django.setup()

    from runner.supervisor import Supervisor

    asyncio.run(Supervisor(role=args.role).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
