# Base con Chromium già installato
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

# Dipendenze Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Codice
COPY . .

# Porta per Render
ENV PORT=10000

# Avvio
CMD ["bash", "start.sh"]
