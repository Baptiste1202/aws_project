FROM python:3.12-slim

RUN pip install --no-cache-dir boto3 psycopg2-binary

COPY src/vm/app.py local/init_aws.py local/auth_local.py local/stripe_local.py data/products.json \
     infra/templates/fulfillment.asl.json infra/templates/restock.asl.json /opt/shop/
COPY src/shop/ /opt/shop/shop/
COPY src/frontend/ /opt/shop/static/
# Code des Lambdas, déployé dans LocalStack par init_aws.py (le même que sur AWS,
# avec le paquet commun shop/ ajouté dans chaque zip).
COPY src/lambda/ /opt/lambda/
# Pillow de la Lambda thumbnailer : même wheel Linux arm64 figée que sur AWS
# (scripts/lambda-requirements.txt). LocalStack lance ses Lambdas dans des conteneurs de
# l'architecture de la machine : stack locale prévue pour un Mac Apple Silicon (arm64).
COPY scripts/lambda-requirements.txt /tmp/lambda-requirements.txt
RUN pip install --no-cache-dir --quiet --target /opt/lambda-deps/thumbnailer \
      --platform manylinux2014_aarch64 --implementation cp --python-version 3.12 \
      --only-binary=:all: --no-deps --require-hashes -r /tmp/lambda-requirements.txt

ENV PYTHONUNBUFFERED=1
CMD ["python", "/opt/shop/app.py"]
