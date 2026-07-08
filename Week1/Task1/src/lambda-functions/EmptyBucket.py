import boto3

s3 = boto3.client("s3")

BUCKET = "image-resize-bucket-ak"

def lambda_handler(event, context):

    print("Starting bucket cleanup...")

    response = s3.list_objects_v2(Bucket=BUCKET)

    if "Contents" not in response:
        print("Bucket is already empty.")
        return {
            "statusCode": 200,
            "body": "Bucket already empty."
        }

    for obj in response["Contents"]:
        print(f"Deleting {obj['Key']}")

        s3.delete_object(
            Bucket=BUCKET,
            Key=obj["Key"]
        )

    print("Cleanup complete!")

    return {
        "statusCode": 200,
        "body": "Bucket emptied successfully."
    }