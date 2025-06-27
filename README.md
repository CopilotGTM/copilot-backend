# User Authentication Service

A FastAPI-based service for user authentication, featuring local development and production-ready configurations, as well as Celery integration for background tasks.

---

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Setup & Installation](#setup--installation)
- [Development](#development)
- [Production](#production)
- [Code Formatting](#code-formatting)

---

## Features

- User authentication APIs built with FastAPI
- Asynchronous background processing with Celery
- Easy local development with hot-reload
- Production-ready configuration

---

## Prerequisites

- Python 3.8+
- [pip](https://pip.pypa.io/en/stable/)
- [Uvicorn](https://www.uvicorn.org/)
- [Celery](https://docs.celeryq.dev/en/stable/)
- (Optional) Redis or RabbitMQ for Celery broker

---

## Setup & Installation

1. **Clone the repository:**
   ```bash
   git clone <your-repo-url>
   cd <repo-directory>
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

---

## Development

To run the FastAPI app and Celery worker locally with hot-reload and debug logging:

```bash
# Start FastAPI server (with reload and debug logging)
uvicorn app_layer.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug

# In a separate terminal, start Celery worker
celery -A celery_app worker --loglevel=info -P solo
```

---

## Production

To run the application in production mode (with multiple workers):

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers $(nproc) --reload
```

---

## Code Formatting

Before pushing code, format it using [Black](https://black.readthedocs.io/en/stable/):

```bash
black . -l 120
```

---
