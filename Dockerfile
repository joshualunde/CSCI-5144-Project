FROM python:3.12-slim
WORKDIR /app
COPY mock_exchange.py /app/mock_exchange.py
ENV PORT=8000
ENV WORK_FACTOR=12000
EXPOSE 8000
CMD ["python", "-u", "mock_exchange.py"]
