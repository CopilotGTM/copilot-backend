FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Allow passing ENVIRONMENT at build time (defaults to "development")
ARG ENVIRONMENT=development

# Copy the matching env file into .env (what most libraries look for)
# e.g. .env.development → .env
COPY .env.${ENVIRONMENT} .env

EXPOSE 80

# Start Uvicorn with the maximum available CPU cores
CMD ["sh", "-c", "uvicorn  app_layer.main:app --host 0.0.0.0 --port 80 --workers 1"]