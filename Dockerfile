# syntax=docker/dockerfile:1
#
# qc.all MCP server - container image
#
# Serves the qc.all Model Context Protocol server over HTTP (streamable-http) so a
# remote MCP client can reach it across the network.  File access is confined to
# /data, which you mount from the host.
#
# Build:
#     docker build -t qcall-mcp .
#
# Run (mount your survey data read-only and publish the port):
#     docker run --rm -p 8000:8000 -v C:\surveydata:/data:ro qcall-mcp
#
# The MCP endpoint is then http://<host>:8000/mcp

FROM python:3.14-slim AS base

# - PYTHONUNBUFFERED so the startup banner / logs appear immediately in `docker logs`
# - PYTHONDONTWRITEBYTECODE keeps the image clean
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System libraries.  rasterio/pyproj ship manylinux wheels that bundle GDAL/PROJ, but
# that bundled GDAL still dynamically links the system expat XML parser (libexpat.so.1),
# which is not present in the slim image - without it rasterio import / GeoTIFF writing
# fails with "libexpat.so.1: cannot open shared object file".  Install it explicitly.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libexpat1 \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first so this layer is cached unless requirements change.
# rasterio, pyproj and scipy ship manylinux wheels that bundle GDAL/PROJ, so only the
# system libexpat1 above is needed on top of a standard linux/amd64 or linux/arm64 build.
COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
 && python -m pip install -r requirements.txt

# Copy the application code.
COPY . .

# Default runtime configuration.  Every value can be overridden at `docker run`
# time with -e, and these map onto qcall_mcp.py's command line options.
#   QCALL_MCP_TRANSPORT  http  -> serve over HTTP (streamable-http)
#   QCALL_MCP_HOST       0.0.0.0 -> listen on all interfaces inside the container
#   QCALL_MCP_PORT       8000
#   QCALL_MCP_ROOT       /data -> file access is confined to this folder
#   QCALL_LOG_DIR        /data/logs -> shared rotating log (visible to the monitor)
ENV QCALL_MCP_TRANSPORT=http \
    QCALL_MCP_HOST=0.0.0.0 \
    QCALL_MCP_PORT=8000 \
    QCALL_MCP_ROOT=/data \
    QCALL_LOG_DIR=/data/logs

# The folder survey data is mounted into and outputs/logs are written to.
RUN mkdir -p /data
VOLUME ["/data"]

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 qcall \
 && chown -R qcall:qcall /app /data
USER qcall

EXPOSE 8000

# Simple TCP health check - the container is healthy once the HTTP server accepts
# connections on the configured port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,socket,sys; s=socket.socket(); s.settimeout(3); sys.exit(s.connect_ex(('127.0.0.1', int(os.environ.get('QCALL_MCP_PORT','8000')))))"

# qcall_mcp.py reads the QCALL_MCP_* environment variables above, so no arguments
# are needed.  Extra flags can still be appended, e.g. `docker run ... qcall-mcp --port 9000`.
ENTRYPOINT ["python", "qcall_mcp.py"]
