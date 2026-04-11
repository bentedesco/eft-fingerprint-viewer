# Stage 1: Build NBIS from official NIST source
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Download and verify NBIS v5.0.0 from NIST
ENV NBIS_URL="https://nigos.nist.gov/nist/nbis/nbis_v5_0_0.zip"
ENV NBIS_SHA256="0adf8ab0f6b0e4208de50ca00ba21d3d77112ecd66288757ddfed21f6bee92c3"

RUN curl -fSL "${NBIS_URL}" -o /tmp/nbis.zip \
    && echo "${NBIS_SHA256}  /tmp/nbis.zip" | sha256sum -c - \
    && mkdir -p /tmp/nbis-src \
    && unzip -q /tmp/nbis.zip -d /tmp/nbis-src \
    && rm /tmp/nbis.zip

# Build NBIS
# -fcommon is required because GCC 10+ defaults to -fno-common, which breaks
# NBIS's tentative definitions (global vars declared in multiple translation units).
# We create a gcc wrapper that injects -fcommon into every compilation.
RUN printf '#!/bin/sh\nexec /usr/bin/gcc.real -fcommon "$@"\n' > /usr/local/bin/gcc-wrapper \
    && chmod +x /usr/local/bin/gcc-wrapper \
    && mv /usr/bin/gcc /usr/bin/gcc.real \
    && ln -s /usr/local/bin/gcc-wrapper /usr/bin/gcc

RUN cd /tmp/nbis-src/Rel_5.0.0 \
    && mkdir -p /opt/nbis \
    && bash setup.sh /opt/nbis --without-X11 --STDLIBS \
    && make config \
    && make it \
    && make install LIBNBIS=no

# Stage 2: Runtime image
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libopenjp2-tools \
    && rm -rf /var/lib/apt/lists/*

# Copy NBIS binaries from builder
COPY --from=builder /opt/nbis/bin /opt/nbis/bin

WORKDIR /app

COPY index.html server.py ./
COPY test_FD-258/ ./test_FD-258/

ENV NBIS_BIN_PATH="/opt/nbis/bin"

EXPOSE 8888

CMD ["python3", "server.py"]
