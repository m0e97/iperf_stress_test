FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IPERF_DATA_DIR=/data

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY webapp ./webapp

RUN mkdir -p /data/uploads /data/reports

EXPOSE 8800

CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8800"]
