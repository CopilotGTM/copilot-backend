#!/usr/bin/env python3
import os
import sys
from alembic.config import Config
from alembic import command


def main():
    # locate your alembic.ini (adjust if it's elsewhere)
    here = os.path.dirname(__file__)
    ini_path = os.path.join(here, "alembic.ini")
    if not os.path.exists(ini_path):
        print("❌  Cannot find alembic.ini at", ini_path, file=sys.stderr)
        sys.exit(1)

    alembic_cfg = Config(ini_path)

    # 1) generate a new revision with autogenerate
    command.revision(
        alembic_cfg,
        message="create initial tables",
        autogenerate=True,
    )

    # 2) upgrade to the latest head
    command.upgrade(alembic_cfg, "head")

    print("✅  Revision created and database upgraded to head.")


if __name__ == "__main__":
    main()
