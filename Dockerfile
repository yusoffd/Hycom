FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Cloud Run sets $PORT; default to 8080 elsewhere.
ENV PORT=8080
EXPOSE 8080
CMD streamlit run app.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true
