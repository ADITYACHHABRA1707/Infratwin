# One image, two entry points: the Streamlit dashboard (default) and the REST API.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first so this layer is cached until requirements change.
COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY . .
RUN useradd --create-home appuser && chown -R appuser /app
USER appuser

# Secrets (DATABASE_URL, JWT_SECRET) are supplied at run time, never baked into the image.
EXPOSE 8501 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

CMD ["python", "-m", "streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
