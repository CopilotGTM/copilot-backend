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

- **Super Admin System**: Secure admin authentication with email notifications
- **Multi-Tenant Architecture**: Organization-based user management
- **AI-Powered Chat**: Intelligent conversations with document search
- **Technical Deck Generation**: Automated presentation creation
- **RFP Processing**: Intelligent document analysis and response generation
- **User authentication APIs** built with FastAPI
- **Asynchronous background processing** with Celery
- **Easy local development** with hot-reload
- **Production-ready configuration**

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

3. **Configure environment variables:**
   Create a `.env` file with the following variables:
   ```bash
   # Database Configuration
   DATABASE_HOST=localhost
   DATABASE_PORT=5432
   DATABASE_USER=postgres
   DATABASE_PASSWORD=your_password
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
   ```

4. **Initialize super admin (first deployment):**
   ```bash
   python scripts/admin/deploy_with_super_admin.py
   ```

5. **Frontend Integration:**
   Use Supabase authentication client in your frontend for admin login:
   ```javascript
   // Example with Supabase client
   const { data, error } = await supabase.auth.signInWithPassword({
     email: 'admin@yourcompany.com',
     password: 'temporary_password'
   })
   ```

---

## Development

To run the FastAPI app and Celery worker locally with hot-reload and debug logging:

```bash
# Start FastAPI server (with reload and debug logging)
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload --log-level debug

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
