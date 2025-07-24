# User Authentication Service

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
