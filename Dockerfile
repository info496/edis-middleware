# Base ufficiale Playwright per Python, versione 1.54.0 su Ubuntu Jammy
FROM mcr.microsoft.com/playwright/python:v1.54.0-jammy

WORKDIR /app

# Dipendenze Python dell'app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Codice applicazione
COPY . .

# Porta per Render
ENV PORT=10000

# Avvio
CMD ["bash", "start.sh"]
