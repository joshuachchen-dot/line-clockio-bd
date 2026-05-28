#!/bin/bash
# deploy.sh — Build and deploy to GCP Cloud Run
# Usage: ./deploy.sh
# Prerequisites: gcloud CLI authenticated, Docker running

set -euo pipefail

PROJECT_ID="aiotek-bot"
REGION="asia-east1"
SERVICE="line-clockio"
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE}"

echo "==> Building Docker image (linux/amd64 for Cloud Run)..."
docker build --platform linux/amd64 -t "${IMAGE}" .

echo "==> Pushing to Google Container Registry..."
docker push "${IMAGE}"

echo "==> Deploying to Cloud Run..."
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --platform managed \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --allow-unauthenticated \
  --min-instances 1 \
  --max-instances 10 \
  --memory 512Mi \
  --cpu 1 \
  --timeout 60 \
  --add-cloudsql-instances "${PROJECT_ID}:${REGION}:line-clockio-db-new" \
  --set-env-vars "DEBUG=${DEBUG:-false}" \
  --set-secrets "\
LINE_CHANNEL_ACCESS_TOKEN=LINE_CHANNEL_ACCESS_TOKEN:latest,\
LINE_CHANNEL_SECRET=LINE_CHANNEL_SECRET:latest,\
LIFF_ID=LIFF_ID:latest,\
LIFF_CHANNEL_ID=LIFF_CHANNEL_ID:latest,\
LIFF_CHANNEL_SECRET=LIFF_CHANNEL_SECRET:latest,\
DATABASE_URL=DATABASE_URL:latest,\
MAILGUN_API_KEY=MAILGUN_API_KEY:latest,\
MAILGUN_FROM_EMAIL=MAILGUN_FROM_EMAIL:latest,\
SESSION_SECRET_KEY=SESSION_SECRET_KEY:latest,\
APP_BASE_URL=APP_BASE_URL:latest,\
FTP_USER=FTP_USER:latest,\
FTP_PASSWORD=FTP_PASSWORD:latest,\
FTP_REMOTE_DIR=FTP_REMOTE_DIR:latest,\
INTERNAL_SECRET=INTERNAL_SECRET:latest"

echo "==> Setting public access policy..."
gcloud beta run services add-iam-policy-binding "${SERVICE}" \
  --region="${REGION}" \
  --member="allUsers" \
  --role="roles/run.invoker" \
  --project="${PROJECT_ID}" || echo "  (IAM policy already set or requires org policy override)"

echo ""
echo "==> Deploy complete."
echo "    Service URL:"
gcloud run services describe "${SERVICE}" \
  --platform managed \
  --region "${REGION}" \
  --project "${PROJECT_ID}" \
  --format "value(status.url)"
