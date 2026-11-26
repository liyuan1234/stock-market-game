FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py dashboard.py db.py config.json index.html login.html ./
EXPOSE 8080
CMD ["sh", "-c", "uvicorn dashboard:app --host 0.0.0.0 --port ${PORT:-8080}"]
