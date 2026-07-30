import os
from google.cloud import firestore

# Set credentials env var
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "lucas-pipeline-2026-v1-ec3e767f8c46.json"

db = firestore.Client()

try:
    print("Connecting to Firestore...")
    projects_ref = db.collection("projects").stream()
    projects = list(projects_ref)
    print(f"Total projects found in Firestore: {len(projects)}")
    for doc in projects:
        data = doc.to_dict()
        print(f" - Project ID: {doc.id}")
        print(f"   Title: {data.get('title')}")
        print(f"   Channel: {data.get('channel')}")
        print(f"   Script Locked: {data.get('script_locked')}")
        print(f"   Storyboard Approved: {data.get('storyboard_approved')}")
        
        # List beats
        beats_ref = db.collection("projects").document(doc.id).collection("beats").stream()
        beats = list(beats_ref)
        print(f"   Beats count: {len(beats)}")
except Exception as e:
    print(f"Error inspecting Firestore: {e}")
