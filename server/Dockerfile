FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir "mcp[cli]>=1.0.0" "redis>=5.0.0" "httpx>=0.27.0" "numpy>=1.26.0"
COPY memory_mcp.py INSTRUCTIONS.md tcp_server.py ./
EXPOSE 3006
# Kubernetes / TCP clients (socat). Local Cursor stdio: python memory_mcp.py
CMD ["python", "tcp_server.py"]
