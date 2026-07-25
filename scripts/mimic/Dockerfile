# The MIMIC showcase tooling image (#68): one-shot ETL runs (fetch/load/push/link/seeder) and
# the long-running ris-sign-bridge service all run from this directory inside the compose
# network. Built by compose (ris-sign-bridge) or by hand for ETL runs:
#   docker build -t mimic-etl scripts/mimic
FROM python:3.11-slim
WORKDIR /etl
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
