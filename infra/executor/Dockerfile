FROM python:3.13-alpine

RUN apk add --no-cache \
        bash \
        coreutils \
        curl \
        findutils \
        git \
    && addgroup -S sandbox \
    && adduser -S -G sandbox sandbox

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /workspace

USER sandbox

CMD ["sleep", "infinity"]
