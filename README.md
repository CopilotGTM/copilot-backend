# Copilot Backend - Production-Grade B2B SaaS

[![Python 3.11](https://img.shields.io/badge/python-3.11-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.116.1-009688.svg)](https://fastapi.tiangolo.com)
[![UV](https://img.shields.io/badge/uv-package%20manager-blue)](https://github.com/astral-sh/uv)

Enterprise-grade B2B SaaS backend built with FastAPI, async support, and AI integrations.

---

## 🚀 Quick Start

### Prerequisites

- **Python 3.11+** - [Download](https://www.python.org/downloads/)
- **UV** - Fast Python package installer ([Installation Guide](https://github.com/astral-sh/uv#installation))
- **Docker & Docker Compose** - For containerized services
- **PostgreSQL 16** with pgvector extension
- **Redis 6+** - For caching and task queue

### Install UV (Fast Package Manager)

**Windows (PowerShell):**
```powershell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS/Linux:**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 📦 Installation

### 1. Clone and Setup Dependencies

```bash
# Install WITH dev dependencies (black, ruff, pytest, etc.) - FOR DEVELOPMENT
uv sync --all-extras

# Run common commands directly
python -m pytest                    # Run tests
python -m black src/ tests/         # Format code
python -m ruff check src/ tests/    # Lint code
python -m uvicorn src.api.fastapi_app:app --reload  # Run dev server
```

**⚠️ IMPORTANT**: Always use `uv sync --all-extras` for development to get dev tools!

📖 **See detailed UV commands and deployment guide**: `scripts/deployment/README.md`

### 2. Start Infrastructure Services

```bash
docker run -d --name copilot-postgres -p 5432:5432 -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=postgres pgvector/pgvector:pg16
docker run -d --name copilot-redis -p 6379:6379 redis:7-alpine
```

### 3. Configure Environment Variables

Create a `.env` file in the project root:

```bash
# Database Configuration
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_USER=postgres
DATABASE_PASSWORD=postgres
DATABASE_NAME=postgres

# Super Admin Configuration
SUPER_ADMIN_EMAIL=admin@yourcompany.com

# Supabase Configuration
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_ROLE_KEY=your-service-role-key
SUPABASE_JWT_SECRET=your-jwt-secret

# Email Configuration (Resend)
RESEND_API_KEY=your-resend-api-key
FROM_EMAIL=noreply@yourdomain.com

# Application Configuration
APP_NAME=CopilotGTM
APP_URL=https://your-app-domain.com

# Azure Configuration (if using Azure services)
AZURE_OPENAI_ENDPOINT=https://your-endpoint.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
```

### 4. Initialize Database Migrations

```bash
# Run master schema migrations
uv run python -m scripts.run_master_migrations

# Deploy super admin (if SUPER_ADMIN_EMAIL is set)
uv run python -m scripts.admin.deploy_with_super_admin

# Initialize organization schema (for testing)
# First create schema manually: CREATE SCHEMA test_org_schema;
uv run python -m alembic -c migrations/org_migrations/alembic.ini -x schema=test_org_schema upgrade head
```

### 5. Run the Application

**Development Mode:**
```bash
uv run uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
```

**Production Mode:**
```bash
uv run uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 4
```

---

## 🐳 Docker Deployment

### Build Production Image

```bash
docker build -t copilot-backend:latest .
```

### Run Container

```bash
docker run -p 8080:80 --env-file .env copilot-backend:latest
```

---

## 🛠️ Development Workflow

### Code Quality Checks

```bash
# Format code with black
uv run black src/ tests/

# Lint with ruff
uv run ruff check src/ tests/

# Type checking with mypy
uv run mypy src/

# Run all checks
uv run black src/ tests/ && uv run ruff check src/ tests/ && uv run mypy src/
```

### Running Tests

```bash
# Run all tests
uv run pytest tests/ -v

# Run with coverage
uv run pytest tests/ -v --cov=src --cov-report=term-missing --cov-report=html

# Run specific test markers
uv run pytest tests/ -v -m unit
uv run pytest tests/ -v -m integration
```

---

## 📚 Dependency Management

### Update Dependencies

```bash
# Update all dependencies
uv lock --upgrade

# Sync with updated lockfile
uv sync
```

### Add New Dependencies

```bash
# Add production dependency
uv add package-name

# Add dev dependency
uv add --dev package-name

# Regenerate lockfile
uv lock
```

### View Dependency Tree

```bash
uv tree
```

---

## 🚢 Production Deployment

### Build Production-Ready Image

```bash
# Clean build
docker build --no-cache -t copilot-backend:production .

# Tag for registry
docker tag copilot-backend:production registry.example.com/copilot-backend:latest

# Push to registry
docker push registry.example.com/copilot-backend:latest
```

### Kubernetes/Helm Deployment

```bash
# Deploy using Helm
helm upgrade --install copilot-backend ./charts/application \
  --namespace production \
  --values ./charts/application/values.yaml
```

---

## 📖 Additional Documentation

- [API Documentation](./docs/API%20Documentation/)
- [Technical Documentation](./docs/Technical%20Documentation/)
- [Functional Documentation](./docs/Functional%20Documentation/)

---

## 🔧 Troubleshooting

### Issue: UV not found

**Solution:** Ensure UV is installed and in your PATH. Restart your terminal after installation.

### Issue: Python version mismatch

**Solution:** This project requires Python 3.11+. UV will automatically use the correct version if available.

```bash
# Check Python version
python --version

# UV will handle version automatically
uv python list
```

### Issue: Database connection errors

**Solution:** Ensure PostgreSQL and Redis containers are running:

```bash
docker ps
docker logs copilot-postgres
docker logs copilot-redis
```
---

## 📝 Helper Commands Reference

---

## 📄 License

Proprietary - All rights reserved

---

## 🔗 Links

- [UV Documentation](https://github.com/astral-sh/uv)
- [FastAPI Documentation](https://fastapi.tiangolo.com)
- [Docker Documentation](https://docs.docker.com)
