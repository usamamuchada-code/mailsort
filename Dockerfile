FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn
COPY . .
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8080
# one worker (background jobs live in-process), many threads, long timeout for big uploads
CMD gunicorn -w 1 --threads 8 --timeout 900 -b 0.0.0.0:${PORT:-8080} app:app
