# Una sola receta, una imagen por microservicio:
#   docker build --build-arg SERVICE=billing -t aegisvolt/billing services/
FROM python:3.12-slim
ARG SERVICE
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common.py local_tables.py ./
COPY ${SERVICE}/ ./
ENV PYTHONUNBUFFERED=1
USER nobody
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
