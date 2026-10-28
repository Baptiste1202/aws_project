"""Miniatures des images produit, déclenchée par S3 (dépôt dans originals/).

  originals/<produit>/<id>.<ext>  ->  images/<produit>/<id>-160.webp et -480.webp

L'original est déposé par le back-office avec un formulaire présigné (5 Mo au plus,
type image/*). Le fichier est vérifié par Pillow avant tout traitement ; un fichier
qui n'est pas une image est ignoré (journalisé). Les miniatures sont servies par
CloudFront sous /images/ (cache long : leur nom change à chaque nouvelle image).
Pillow vient de scripts/lambda-requirements.txt (wheel Linux arm64, empreinte figée).
"""

import io
import json
import re
import urllib.parse

import boto3
from PIL import Image

SIZES = (160, 480)
KEY_RE = re.compile(r"^originals/([A-Z]{2}-\d{3,9})/([0-9a-f]{32})\.(jpg|jpeg|png|webp)$")
Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse les « bombes de décompression »

s3 = boto3.client("s3")


def thumbnails(bucket, key):
    match = KEY_RE.match(key)
    if not match:
        print(json.dumps({"msg": "clé ignorée", "key": key}))
        return []
    product_id, image_id, _ = match.groups()
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    try:
        Image.open(io.BytesIO(data)).verify()  # fichier tronqué ou pas une image : exception
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"msg": "pas une image valide, ignorée", "key": key, "error": str(e)}))
        return []
    image = image.convert("RGBA" if image.mode in ("RGBA", "LA", "P") else "RGB")
    written = []
    for size in SIZES:
        copy = image.copy()
        copy.thumbnail((size, size))
        out = io.BytesIO()
        copy.save(out, "WEBP", quality=80, method=4)
        target = f"images/{product_id}/{image_id}-{size}.webp"
        s3.put_object(Bucket=bucket, Key=target, Body=out.getvalue(), ContentType="image/webp",
                      CacheControl="public, max-age=31536000, immutable")
        written.append({"key": target, "width": copy.width, "height": copy.height})
    print(json.dumps({"msg": "miniatures écrites", "source": key, "miniatures": written}))
    return written


def handler(event, context):
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        thumbnails(bucket, key)
    return {"ok": True}
