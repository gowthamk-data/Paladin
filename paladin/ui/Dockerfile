# Single stage: every pinned dependency ships a prebuilt manylinux wheel, so no compiler
# toolchain (and no apt layer at all) is needed.
FROM python:3.11.16-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY ui/requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
    && pip install -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt \
    && find /usr/local/lib/python3.11/site-packages -type d -name __pycache__ -prune -exec rm -rf {} +

WORKDIR /app
COPY ui/ ./ui/

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin paladin \
    && chown -R paladin:paladin /app

USER paladin

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)" || exit 1

CMD ["streamlit", "run", "ui/app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
