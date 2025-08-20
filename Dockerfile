FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Create log directory
RUN mkdir -p /var/log

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Copy entrypoint and make executable
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Set environment variables for better logging and connection handling
ENV PYTHONUNBUFFERED=1
ENV CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP=true
ENV CELERY_BROKER_CONNECTION_MAX_RETRIES=10
ENV CELERY_BROKER_CONNECTION_RETRY_INTERVAL=5

# Environment variables will be loaded at runtime from .env

EXPOSE 80

# Start Uvicorn with the maximum available CPU cores
ENTRYPOINT ["sh", "./entrypoint.sh"]

