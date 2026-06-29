FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    IPERF_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY clock.py ./
COPY secret_store.py ./
COPY webapp ./webapp

EXPOSE 8800

CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8800"]
