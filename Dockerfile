FROM python:3.10-slim

WORKDIR /app

# Install git/curl needed by playwright installer
RUN apt-get update && apt-get install -y git curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Automatically installs Chromium AND all required system dependencies
RUN playwright install --with-deps chromium

COPY . .

EXPOSE 10000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]
