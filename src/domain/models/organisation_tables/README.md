# Organisation Tables Models

This directory contains the SQLModel definitions for the organisation-specific database tables. These models define the schema for storing data related to chat sessions, messages, knowledge base, and other organisation-specific data.

## Overview

The models are organised into separate files based on their domain:

-   [`chat_session_models.py`](chat_session_models.py:1): Manages chat sessions.
-   [`chat_message_models.py`](chat_message_models.py:1): Manages chat messages and attachments.
-   [`knowledge_models.py`](knowledge_models.py:1): Manages knowledge base documents and their embeddings.
-   [`job_models.py`](job_models.py:1): Manages background jobs for chat messages and knowledge processing.
-   [`base_mixins.py`](base_mixins.py:1): Provides common mixin classes for timestamps.

For a detailed explanation of the database schema, table relationships, and production-grade considerations, please refer to the [Organisation Database Schema Documentation](../../../../docs/organisation-database-schema.md).