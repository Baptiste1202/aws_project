FROM python:3.12-slim

RUN pip install --no-cache-dir boto3 psycopg2-binary

COPY src/vm/app.py local/init_aws.py data/products.json /opt/shop/
COPY src/frontend/ /opt/shop/static/
# Code des Lambdas, déployé dans LocalStack par init_aws.py (le même que sur AWS).
COPY src/lambda/ /opt/lambda/

ENV PYTHONUNBUFFERED=1
CMD ["python", "/opt/shop/app.py"]
