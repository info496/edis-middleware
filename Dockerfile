# Usa l'immagine Playwright ufficiale (include Chromium e dipendenze)
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

# Dipendenze Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Codice
COPY . .

ENV PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port $PORT"]
