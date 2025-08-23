FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Installa Chromium e le librerie necessarie per Playwright
RUN python -m playwright install --with-deps chromium

# codice applicazione
COPY . .

# Render fornisce la porta in $PORT
ENV PORT=10000

# Avvio
CMD ["bash", "start.sh"]
