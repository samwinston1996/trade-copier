FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY router.py channel_state.py .
CMD ["uvicorn", "router:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "4", "--http", "h11"]
