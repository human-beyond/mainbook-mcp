FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MAINBOOK_MCP_TRANSPORT=http \
    MAINBOOK_MCP_HOST=0.0.0.0 \
    MAINBOOK_MCP_PORT=8000

WORKDIR /app

RUN addgroup --system mainbook && adduser --system --ingroup mainbook mainbook

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

USER mainbook
EXPOSE 8000

CMD ["mainbook-mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
