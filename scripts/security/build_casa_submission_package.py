import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from scripts.security.check_casa_submission_preflight import format_report, run_preflight

ROOT_FILES = [
    "README.md",
    "evidence-log.md",
    "google-oauth-scope-inventory.md",
    "secrets-baseline-audit.md",
]

EVIDENCE_DIRS = [
    "runs",
    "pipeline-runs",
    "infra-pipeline-runs",
    "dast-runs",
]


def _copy_file_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _copy_dir_if_exists(source: Path, destination: Path) -> None:
    if source.exists():
        shutil.copytree(source, destination, dirs_exist_ok=True)


def build_package(
    *,
    casa_root: Path,
    output_root: Path,
    package_name: str | None = None,
    allow_incomplete: bool = False,
) -> Path:
    result = run_preflight(casa_root)
    if not result.passed and not allow_incomplete:
        raise RuntimeError("CASA submission preflight failed:\n" + format_report(result))

    if package_name is None:
        package_name = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    package_path = output_root / package_name
    package_path.mkdir(parents=True, exist_ok=False)

    for filename in ROOT_FILES:
        _copy_file_if_exists(casa_root / filename, package_path / filename)

    for dirname in EVIDENCE_DIRS:
        _copy_dir_if_exists(casa_root / "evidence" / dirname, package_path / "evidence" / dirname)

    manifest = [
        "CASA submission package",
        f"GeneratedAt={datetime.now(UTC).isoformat()}",
        f"Source={casa_root}",
        f"Preflight={'PASS' if result.passed else 'FAIL'}",
        "",
        format_report(result).strip(),
        "",
    ]
    (package_path / "SUBMISSION-MANIFEST.txt").write_text("\n".join(manifest), encoding="utf-8")
    return package_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a CASA assessor submission package.")
    parser.add_argument("--casa-root", type=Path, default=Path("infra/security/casa"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("infra/security/casa/evidence/submission-packages"),
    )
    parser.add_argument("--package-name")
    parser.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="Build a draft package even when preflight fails.",
    )
    args = parser.parse_args()

    try:
        package_path = build_package(
            casa_root=args.casa_root,
            output_root=args.output_root,
            package_name=args.package_name,
            allow_incomplete=args.allow_incomplete,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"CASA submission package created: {package_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
