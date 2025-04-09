FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 5000

# Start Uvicorn with the maximum available CPU cores
CMD ["sh", "-c", "uvicorn  app_layer.main:app --host 0.0.0.0 --port 8000 --workers 4"]