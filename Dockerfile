FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create log directory
RUN mkdir -p /var/log

# Copy only requirements first to leverage Docker cache
COPY requirements.txt ./

# Install dependencies (this layer will be cached unless requirements files change)
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Environment variables will be loaded at runtime from .env

EXPOSE 80

# Start Uvicorn directly
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "80", "--workers", "1", "--log-level", "info"]

