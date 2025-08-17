# Master Tables Models

This directory contains the SQLModel definitions for the master database tables. These models define the schema for storing data related to organisations, users, and super admins.

## Overview

The models are organised into separate files based on their domain:

-   [`organisation_models.py`](organisation_models.py:1): Manages organisations and their action history.
-   [`user_models.py`](user_models.py:1): Manages users and their action history.
-   [`super_admin_models.py`](super_admin_models.py:1): Manages super admins and their roles.
-   [`base_mixins.py`](base_mixins.py:1): Provides common mixin classes for timestamps.

For a detailed explanation of the database schema, table relationships, and production-grade considerations, please refer to the [Master Database Schema Documentation](../../../../docs/master-database-schema.md).