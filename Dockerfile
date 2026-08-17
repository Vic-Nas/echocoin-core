FROM python:3.11-slim

RUN apt-get update -qq && apt-get install -y --no-install-recommends \
    cmake ninja-build libssl-dev git build-essential pkg-config \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/open-quantum-safe/liboqs.git /tmp/liboqs \
    && cmake -S /tmp/liboqs -B /tmp/liboqs/build \
        -DOQS_ALGS_ENABLED=STD \
        -DBUILD_SHARED_LIBS=ON \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
    && cmake --build /tmp/liboqs/build -j$(nproc) \
    && cmake --install /tmp/liboqs/build \
    && ldconfig \
    && rm -rf /tmp/liboqs

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8333
CMD ["python", "main.py", "--log-level", "DEBUG", "--keyfile", "/data/echocoin_key.json", "--db", "/data/echocoin_chain.db"]
