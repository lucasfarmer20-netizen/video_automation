import os
from google.auth import default
from google.auth.transport.requests import Request
import google.auth

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "lucas-pipeline-2026-v1-ec3e767f8c46.json"

credentials, project = google.auth.default(
    scopes=['https://www.googleapis.com/auth/cloud-platform']
)
credentials.refresh(Request())

import requests

service_name = "youtube-video-pipeline"
region = "us-central1"
project_id = "lucas-pipeline-2026-v1"

url = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project_id}/services/{service_name}"
headers = {
    "Authorization": f"Bearer {credentials.token}",
    "Content-Type": "application/json"
}

try:
    print(f"Requesting Cloud Run service status: {url}...")
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        data = resp.json()
        print("Service found!")
        print(f" - Latest Created Revision: {data['status'].get('latestCreatedRevisionName')}")
        print(f" - Latest Ready Revision: {data['status'].get('latestReadyRevisionName')}")
        
        # Print traffic splits
        print("Traffic Splits:")
        traffic = data['status'].get('traffic', [])
        for t in traffic:
            print(f"  - Revision: {t.get('revisionName')} -> Percent: {t.get('percent')}% (latest: {t.get('latestRevision')})")
            
        # Print conditions
        print("Conditions:")
        conditions = data['status'].get('conditions', [])
        for c in conditions:
            print(f"  - Type: {c.get('type')}, Status: {c.get('status')}, Reason: {c.get('reason')}, Message: {c.get('message')}")
    else:
        print(f"Failed to fetch status: {resp.status_code} - {resp.text}")
except Exception as e:
    print(f"Error checking Cloud Run: {e}")
