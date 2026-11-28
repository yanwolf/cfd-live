FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data PORT=8080 TZ=UTC
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY instances.json instances.live.json ./
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "app.main"]
