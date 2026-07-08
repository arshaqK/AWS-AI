import boto3
from PIL import Image
from io import BytesIO

s3 = boto3.client("s3")

DEST_BUCKET = "image-resize-bucket-ak"

def lambda_handler(event, context):

    record = event["Records"][0]

    source_bucket = record["s3"]["bucket"]["name"]
    key = record["s3"]["object"]["key"]

    print(f"Processing {key}")

    response = s3.get_object(
        Bucket=source_bucket,
        Key=key
    )

    image_data = response["Body"].read()

    image = Image.open(BytesIO(image_data))

    resized_image = image.resize((300, 300))

    buffer = BytesIO()

    image_format = image.format if image.format else "JPEG"

    resized_image.save(buffer, format=image_format)

    buffer.seek(0)

    s3.put_object(
        Bucket=DEST_BUCKET,
        Key=key,
        Body=buffer.getvalue(),
        ContentType=response["ContentType"]
    )

    print(f"Successfully resized and uploaded {key}")

    return {
        "statusCode": 200
    }