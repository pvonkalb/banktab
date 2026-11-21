FROM python:3.13-slim

# BUDGET_SECURE_COOKIE: the app marks cookies HTTPS-only by itself when it sees HTTPS
# (directly, or via X-Forwarded-Proto from a proxy/tunnel); set 1 to force it.
# FORWARDED_ALLOW_IPS: which proxies may set X-Forwarded-* headers. "*" is fine when the
# container is only reachable through your proxy/tunnel; narrow it if port 8000 is exposed.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BUDGET_DATA_DIR=/data \
    FORWARDED_ALLOW_IPS=*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 budget \
 && mkdir /data && chown budget /data

COPY app ./app
COPY tools/manage.py tools/backup.py ./tools/

USER budget
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
