# User Authentication Service

## Prerequisites

- Python 3.11
- [pip](https://pip.pypa.io/en/stable/)
- [Uvicorn](https://www.uvicorn.org/)
- [Celery](https://docs.celeryq.dev/en/stable/)
- Redis
- PostgreSQL

---

## Setup & Installation

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

4. **Run Master Migrations:**
   ```bash
   python -m scripts.run_master_migrations
   ```

5. **Deploy with Super Admin:**
   ```bash
   python -m scripts.admin.deploy_with_super_admin
   ```

6. **Initialize Master Schema Migrations:**
   ```bash
   python -m alembic -c migrations/master_migrations/alembic.ini revision --autogenerate -m "initial setup";
   python -m scripts.run_master_migrations
   ```

7. **Initialize Organization Schema Migrations:**
   "test_org_schema" this schema is for testing purpose. Need to be created manully
   ```bash
   python -m alembic -c migrations/org_migrations/alembic.ini -x schema=test_org_schema revision --autogenerate -m "initial setup";
   python -m alembic -c migrations/org_migrations/alembic.ini -x schema=test_org_schema upgrade head ;
   ```