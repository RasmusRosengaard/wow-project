FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
COPY static ./static
COPY alembic ./alembic
COPY alembic.ini .
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh
EXPOSE 8000
# DEFAULT_SELL (UI prefill only, not a hard limit) and PORT are read at
# startup -- Railway injects PORT itself; DEFAULT_SELL defaults to Draenor.
ENTRYPOINT ["./docker-entrypoint.sh"]
