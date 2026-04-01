FROM python:3.12-slim

WORKDIR /app

# Системные зависимости: ssh-клиент нужен для paramiko (sftp), redis-tools опционально
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Директория для данных (AOF, fernet.key, uploads) — монтируется как volume
RUN mkdir -p /app/data/redis /app/data/uploads

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
