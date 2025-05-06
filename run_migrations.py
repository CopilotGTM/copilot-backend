#!/usr/bin/env python3
import os
import sys
from alembic.config import Config
from alembic import command
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

def main():
    # locate your alembic.ini (adjust if it's elsewhere)
    here = os.path.dirname(__file__)
    ini_path = os.path.join(here, "alembic.ini")
    if not os.path.exists(ini_path):
        print("❌  Cannot find alembic.ini at", ini_path, file=sys.stderr)
        sys.exit(1)

    alembic_cfg = Config(ini_path)

    try:
        # Try to generate a new revision with autogenerate
        command.revision(
            alembic_cfg,
            message="create initial tables",
            autogenerate=True,
        )

        # Upgrade to the latest head
        command.upgrade(alembic_cfg, "head")

        print("✅  Revision created and database upgraded to head.")
    except Exception as e:
        # If there's an error (like no previous revisions), try creating a fresh migration
        if "Can't locate revision" in str(e):
            print("No previous revisions found. Creating initial migration...")
            # Create a new revision from scratch
            command.revision(
                alembic_cfg,
                message="create initial tables",
                autogenerate=True,
                depends_on=None,
                rev_id=None,
            )
            command.upgrade(alembic_cfg, "head")
            print("✅  Initial revision created and database upgraded to head.")
        else:
            raise


if __name__ == "__main__":
    main()
