import os
from google.cloud import storage

# Set credentials env var
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "lucas-pipeline-2026-v1-ec3e767f8c46.json"

client = storage.Client()
bucket_name = "lucas-storyboard-vault-001-483921"

try:
    bucket = client.get_bucket(bucket_name)
    print(f"Connected to bucket: {bucket_name}")
    
    # List first 50 blobs in the bucket
    blobs = list(bucket.list_blobs(max_results=50))
    print(f"Total blobs listed (max 50): {len(blobs)}")
    for blob in blobs:
        print(f" - {blob.name} (size: {blob.size} bytes)")
except Exception as e:
    print(f"Error inspecting bucket {bucket_name}: {e}")
