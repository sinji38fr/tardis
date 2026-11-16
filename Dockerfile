FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client rsync rsnapshot && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
RUN useradd --uid 10001 --create-home tardis && mkdir -p /data /secrets /backups /database-backups /public-keys && chown tardis:tardis /data /secrets /backups /database-backups /public-keys
ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="TARDIS" org.opencontainers.image.version="1.0.0" org.opencontainers.image.source="https://github.com/sinji38fr/tardis" org.opencontainers.image.revision=$VCS_REF org.opencontainers.image.licenses="MIT"
COPY app ./app
USER tardis
ENV TARDIS_DATA_DIR=/data TARDIS_SECRET_DIR=/secrets
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]

FROM runtime AS test
USER root
RUN apt-get update && apt-get install -y --no-install-recommends openssh-server && mkdir -p /run/sshd && usermod -p NP root && rm -rf /var/lib/apt/lists/*
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
