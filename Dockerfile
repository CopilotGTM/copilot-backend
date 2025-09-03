FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create log directory
RUN mkdir -p /var/log

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Environment variables will be loaded at runtime from .env

EXPOSE 80

# Start Uvicorn directly
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "80", "--workers", "1", "--log-level", "info"]

