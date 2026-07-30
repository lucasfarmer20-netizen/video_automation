$env:CLOUDSDK_PYTHON = "C:\Users\Lucas_Admin\AppData\Local\Programs\Python\Python312\python.exe"
$gcloud = "C:\Users\Lucas_Admin\google-cloud-sdk\bin\gcloud.cmd"
$proj = "lucas-pipeline-2026-v1"
$bucket = "lucas-storyboard-vault-001-483921"
$keyFile = "lucas-pipeline-2026-v1-ec3e767f8c46.json"

Write-Host "Checking GCP authentication..."
& $gcloud auth activate-service-account --key-file=$keyFile
& $gcloud config set project $proj

Write-Host "Checking if APIs can be enabled..."
$apiResult = & $gcloud services enable cloudbuild.googleapis.com run.googleapis.com artifactregistry.googleapis.com storage.googleapis.com iam.googleapis.com 2>&1
$apiResultStr = Out-String -InputObject $apiResult

if ($apiResultStr -like "*billing-enabled*" -or $apiResultStr -like "*UREQ_PROJECT_BILLING_NOT_FOUND*") {
    Write-Host "Billing is not yet enabled on the project. Waiting for user to link billing account..."
    exit 1
}

Write-Host "APIs enabled successfully! Proceeding with bucket creation and permissions..."

# 1. Create storage bucket if it doesn't exist
$bucketExists = & $gcloud storage buckets describe gs://$bucket --project=$proj 2>&1
$bucketExistsStr = Out-String -InputObject $bucketExists
if ($bucketExistsStr -like "*not found*" -or $bucketExistsStr -like "*BucketNotFound*" -or $bucketExistsStr -like "*does not exist*") {
    Write-Host "Creating bucket gs://$bucket..."
    & $gcloud storage buckets create gs://$bucket --project=$proj --location=us-central1
} else {
    Write-Host "Bucket gs://$bucket already exists."
}

# 2. Get project number
$projNum = & $gcloud projects describe $proj --format="value(projectNumber)"
$projNum = $projNum.Trim()

Write-Host "Project number is $projNum"

# 3. Grant IAM roles to Cloud Build service account (gracefully handle policy update failures)
Write-Host "Configuring Cloud Build service account permissions..."
$iam1 = & $gcloud projects add-iam-policy-binding $proj --member="serviceAccount:$projNum@cloudbuild.gserviceaccount.com" --role="roles/run.admin" 2>&1
$iam1Str = Out-String -InputObject $iam1
$iam2 = & $gcloud projects add-iam-policy-binding $proj --member="serviceAccount:$projNum@cloudbuild.gserviceaccount.com" --role="roles/iam.serviceAccountUser" 2>&1
$iam2Str = Out-String -InputObject $iam2

if ($iam1Str -like "*Policy update access denied*" -or $iam2Str -like "*Policy update access denied*") {
    Write-Warning "Policy update access denied. The service account does not have permission to modify IAM policies."
    Write-Warning "Please manually grant the roles 'Cloud Run Admin' and 'Service Account User' to the Cloud Build service account ($projNum@cloudbuild.gserviceaccount.com) in the Google Cloud Console."
}

# 4. Grant IAM role to Default Compute Engine service account for Storage
Write-Host "Configuring Compute Engine default service account storage permissions..."
$iam3 = & $gcloud storage buckets add-iam-policy-binding gs://$bucket --member="serviceAccount:$projNum-compute@developer.gserviceaccount.com" --role="roles/storage.objectAdmin" 2>&1
$iam3Str = Out-String -InputObject $iam3

if ($iam3Str -like "*Policy update access denied*" -or $iam3Str -like "*403*") {
    Write-Warning "Could not configure storage permissions automatically. Please ensure the Default Compute Engine Service Account ($projNum-compute@developer.gserviceaccount.com) has permissions to access the bucket gs://$bucket."
}

# 5. Get current git commit SHA
$commitSha = git rev-parse HEAD
$commitSha = $commitSha.Trim()

Write-Host "Submitting Cloud Build for commit $commitSha using staging bucket gs://$bucket/source..."
$buildResult = & $gcloud builds submit --config cloudbuild.yaml --gcs-source-staging-dir="gs://$bucket/source" --substitutions=COMMIT_SHA=$commitSha 2>&1
$buildResultStr = Out-String -InputObject $buildResult

# Write output to log file
$logPath = "scratch/deploy_log.txt"
$buildResultStr | Out-File $logPath

if ($buildResultStr -like "*SUCCESS*") {
    Write-Host "Deployment completed successfully!"
    
    # Get deployment URL
    $serviceUrl = & $gcloud run services describe youtube-video-pipeline --region=us-central1 --format="value(status.url)"
    $serviceUrl = $serviceUrl.Trim()
    
    "SUCCESS|$serviceUrl" | Out-File "scratch/deploy_status.txt"
    exit 0
} else {
    Write-Host "Deployment build failed."
    "FAILED" | Out-File "scratch/deploy_status.txt"
    exit 2
}
