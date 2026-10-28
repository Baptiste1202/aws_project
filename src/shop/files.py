"""Fichiers dans S3 par URL présignée : import / export CSV et factures (bucket « data »), images.

En local, les URL doivent être joignables depuis la machine : S3_PUBLIC_ENDPOINT
(http://localhost:4566) sert alors à les signer, à la place de l'adresse interne
de LocalStack.
"""

import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.config import Config

URL_SECONDS = 300


class S3Files:
    def __init__(self, bucket):
        self.bucket = bucket
        self.s3 = boto3.client("s3")
        public = os.environ.get("S3_PUBLIC_ENDPOINT")
        if public:
            self.signer = boto3.client("s3", endpoint_url=public, config=Config(s3={"addressing_style": "path"}))
        else:
            # Adresse régionale (bucket.s3.<région>.amazonaws.com) : avec l'adresse globale,
            # S3 redirige les requêtes vers un bucket récent pendant la propagation DNS, et la
            # signature, calculée pour l'hôte d'origine, ne correspond plus (403).
            region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
            self.signer = boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com",
                                       config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))

    def export_csv(self, text):
        key = f"exports/catalog-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.csv"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"),
                           ContentType="text/csv; charset=utf-8")
        url = self.signer.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                                 ExpiresIn=URL_SECONDS)
        return {"key": key, "url": url, "expires_in": URL_SECONDS}

    def import_url(self):
        key = f"imports/{uuid.uuid4().hex}.csv"
        url = self.signer.generate_presigned_url("put_object", Params={"Bucket": self.bucket, "Key": key,
                                                                       "ContentType": "text/csv"},
                                                 ExpiresIn=URL_SECONDS)
        return {"key": key, "url": url, "method": "PUT", "content_type": "text/csv", "expires_in": URL_SECONDS}

    def put_invoice(self, order_id, pdf):
        """Facture (régénérée à chaque demande : le bucket ne la garde qu'un jour)."""
        key = f"invoices/{order_id}.pdf"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=pdf, ContentType="application/pdf",
                           ContentDisposition=f'inline; filename="facture-{order_id}.pdf"')
        url = self.signer.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                                 ExpiresIn=URL_SECONDS)
        return {"key": key, "url": url, "expires_in": URL_SECONDS}

    def put_report(self, backend, name, text):
        """Rapport quotidien (lot 4) : reports/<solution>/<fichier>.csv, lien présigné."""
        key = f"reports/{backend}/{name}"
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"), ContentType="text/csv",
                           ContentDisposition=f'attachment; filename="{name}"')
        url = self.signer.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                                 ExpiresIn=URL_SECONDS)
        return {"key": key, "url": url, "expires_in": URL_SECONDS}

    def read_import(self, key):
        if not key.startswith("imports/") or ".." in key:
            raise ValueError("clé d'import invalide")
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8-sig")


IMAGE_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


class ImageFiles(S3Files):
    """Images produit : l'original est déposé par un formulaire présigné (POST), qui
    impose la clé, le type et la taille (contrairement à un PUT présigné) ; la Lambda
    thumbnailer produit ensuite les miniatures servies sous /images/."""

    def upload_form(self, product_id, content_type):
        ext = IMAGE_TYPES.get(content_type)
        if not ext:
            raise ValueError(f"type d'image refusé (attendu : {', '.join(IMAGE_TYPES)})")
        image_id = uuid.uuid4().hex
        key = f"originals/{product_id}/{image_id}.{ext}"
        form = self.signer.generate_presigned_post(
            self.bucket, key, Fields={"Content-Type": content_type},
            Conditions=[{"Content-Type": content_type}, ["content-length-range", 1, MAX_IMAGE_BYTES]],
            ExpiresIn=URL_SECONDS)
        return {"image_id": image_id, "key": key, "url": form["url"], "fields": form["fields"],
                "max_bytes": MAX_IMAGE_BYTES, "expires_in": URL_SECONDS}

    def original_exists(self, product_id, image_id):
        found = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=f"originals/{product_id}/{image_id}.", MaxKeys=1)
        return found.get("KeyCount", 0) > 0
