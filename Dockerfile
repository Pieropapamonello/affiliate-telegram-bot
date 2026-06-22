FROM python:3.11-slim

WORKDIR /app

# Font per le immagini personalizzate (Pillow)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Modello scontorno AI (U2Net-p ~4.5MB) pre-scaricato: nessun download a runtime
RUN mkdir -p /app/models && \
    python -c "import urllib.request; urllib.request.urlretrieve('https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx', '/app/models/u2netp.onnx')"

# Copy bot script
COPY main.py .

# Run the bot
CMD ["python", "main.py"]
