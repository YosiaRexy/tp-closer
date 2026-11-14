# Dockerfile - Fly.io build
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY bot.py .
COPY index.html .
COPY manifest.json .
COPY sw.js .
COPY icon.svg .

# Environment
ENV PORT=8080
ENV DB_PATH=/data/bot.db

# Expose port
EXPOSE 8080

# Run
CMD ["python", "bot.py"]