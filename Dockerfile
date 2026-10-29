FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    TZ=UTC

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-dejavu-core \
        gosu \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && gosu nobody true

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ARG GIT_COMMIT=unknown
ARG GIT_COMMIT_TITLE=unknown
RUN printf 'commit=%s\ntitle=%s\n' "$GIT_COMMIT" "$GIT_COMMIT_TITLE" > /app/.build-git \
    && mkdir -p /app/data /app/backups /tmp/matplotlib \
    && useradd --create-home --uid 1000 botuser \
    && chown -R botuser:botuser /app /tmp/matplotlib \
    && chmod +x /app/docker-entrypoint.sh

# Start as root so the entrypoint can chown bind-mounted volumes, then drop to botuser.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "bot.py"]
