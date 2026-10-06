FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /sim
COPY requirements-simulator.txt .
RUN pip install --no-cache-dir -r requirements-simulator.txt
COPY tools/ocpp_simulator.py ./ocpp_simulator.py
ENTRYPOINT ["python", "/sim/ocpp_simulator.py"]
