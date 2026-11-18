# Railway（railway.json）與 Zeabur 皆以此 Dockerfile 建置；本服務是長駐 worker，不開 HTTP 埠
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Asia/Taipei
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "main.py"]
