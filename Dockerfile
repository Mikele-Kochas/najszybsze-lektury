FROM python:3.12-slim

# ffmpeg jest wymagany do cięcia audio i do próbek używanych przy kotwicach rozdziałów.
# curl służy healthcheckowi poniżej.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Opcjonalne dodatkowe certyfikaty CA. Katalog certs/ jest domyślnie pusty i wtedy ten krok
# nic nie zmienia. Ma znaczenie tylko w sieciach z przechwytywaniem TLS (antywirus ze
# skanowaniem HTTPS, firmowe proxy) - bez zaufanego korzenia pip nie pobierze zależności.
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN find /usr/local/share/ca-certificates/extra -name '*.crt' -type f | grep -q . \
    && update-ca-certificates \
    || echo "Brak dodatkowych certyfikatów CA - używam domyślnego magazynu."

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# faster-whisper (ctranslate2) liczy na GPU przez cuBLAS i cuDNN, których nie ma
# w obrazie slim. Instalujemy je domyślnie - aplikacja jest przeznaczona na maszyny z GPU.
# Build bez nich (lżejszy obraz, praca na CPU): --build-arg WITH_CUDA=false
ARG WITH_CUDA=true
RUN if [ "$WITH_CUDA" = "true" ]; then \
        pip install --no-cache-dir nvidia-cublas-cu12 "nvidia-cudnn-cu12>=9.0"; \
    fi
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib

COPY . .

# Katalogi danych powstają też w kodzie, ale tworzymy je tutaj, żeby obraz działał
# również bez podmontowanego wolumenu.
RUN mkdir -p Data/Text Data/Audio Data/Cache_Transcripts Data/Processed_JSON Data/Output_Packages

ENV PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    # huggingface_hub pobiera modele przez requests, który domyślnie ufa wyłącznie
    # pakietowi certifi i nie widzi certyfikatów dodanych do magazynu systemowego.
    # Bez tych dwóch zmiennych pobranie modelu Whispera zawodzi w sieciach
    # z przechwytywaniem TLS, mimo poprawnie zainstalowanego korzenia CA.
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/api/project" || exit 1

CMD ["python", "Interface/server.py"]
