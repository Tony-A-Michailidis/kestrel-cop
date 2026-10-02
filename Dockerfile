FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY kestrelcop ./kestrelcop
RUN pip install --no-cache-dir .

# config/kestrel.yaml is mounted at runtime; secrets arrive as environment variables
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
ENTRYPOINT ["kestrel"]
CMD ["demo", "--host", "0.0.0.0", "--port", "8000"]
