"""Generate runtime signing secrets for local/dev/prod setup.

Examples:
    python scripts/security/generate_signing_secret.py --target session
    python scripts/security/generate_signing_secret.py --target oauth --format hex
    python scripts/security/generate_signing_secret.py --target all
"""

from __future__ import annotations

import argparse
import secrets
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretDescriptor:
    cli_name: str
    env_var: str
    key_vault_name: str
    description: str


SECRET_DESCRIPTORS = {
    "session": SecretDescriptor(
        cli_name="session",
        env_var="SESSION_SECRET",
        key_vault_name="session-secret",
        description="Starlette session signing secret",
    ),
    "oauth": SecretDescriptor(
        cli_name="oauth",
        env_var="SECRET_KEY",
        key_vault_name="oauth-state-secret-key",
        description="OAuth state JWT signing secret",
    ),
    "voice": SecretDescriptor(
        cli_name="voice",
        env_var="VOICE_REALTIME_CONFIG_SECRET",
        key_vault_name="voice-realtime-config-secret",
        description="Voice realtime runtime config signing secret",
    ),
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one or more signing secrets for local dev or Azure Key Vault."
    )
    parser.add_argument(
        "--target",
        choices=["session", "oauth", "voice", "all"],
        default="session",
        help="Which signing secret to generate.",
    )
    parser.add_argument(
        "--format",
        choices=["urlsafe", "hex"],
        default="urlsafe",
        help="Output encoding for the generated secret.",
    )
    parser.add_argument(
        "--bytes",
        type=int,
        default=32,
        help="Number of random bytes to generate before encoding. Default: 32.",
    )
    return parser


def _generate_secret(*, output_format: str, size_bytes: int) -> str:
    if output_format == "hex":
        return secrets.token_hex(size_bytes)
    return secrets.token_urlsafe(size_bytes)


def _selected_descriptors(target: str) -> list[SecretDescriptor]:
    if target == "all":
        return [
            SECRET_DESCRIPTORS["session"],
            SECRET_DESCRIPTORS["oauth"],
            SECRET_DESCRIPTORS["voice"],
        ]
    return [SECRET_DESCRIPTORS[target]]


def main() -> None:
    args = _build_parser().parse_args()

    for descriptor in _selected_descriptors(args.target):
        value = _generate_secret(output_format=args.format, size_bytes=args.bytes)
        print(f"# {descriptor.description}")
        print(f"# Azure Key Vault secret name: {descriptor.key_vault_name}")
        print(f"# Application env var: {descriptor.env_var}")
        print(f"{descriptor.env_var}={value}")
        print()


if __name__ == "__main__":
    main()
