#!/bin/bash


REGION="us-central1"
PROJECT_ID="verizon-catalyst-poc"
ENVIRONMENT="verizon-catalyst-repo"
PREFIX="engineer-agent"
RUNNER_NAME="${PREFIX}-runner"
IMAGE_NAME="${PREFIX}-image"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$ENVIRONMENT/$IMAGE_NAME:latest"
PORT="8080"
SERVICE_ACCOUNT="techm-dev@poc-z-in2300756.iam.gserviceaccount.com"

# log in to gcloud
gcloud auth activate-service-account --key-file=../credentials/gcp.json
gcloud config set project $PROJECT_ID
gcloud config set run/region $REGION

# Create docker repository
gcloud artifacts repositories create $ENVIRONMENT --repository-format=docker --location=$REGION --description="Docker repository"

# Build the docker image
cp -r ../runner .
gcloud builds submit --config cloudbuild.yaml --substitutions _IMAGE=$IMAGE
rm -rf runner

# Deploy the image to google cloud run with network and port
gcloud run deploy $RUNNER_NAME --image $IMAGE --platform managed \
  --region $REGION \
  --memory 16Gi \
  --cpu 8 \
  --no-allow-unauthenticated \
  --port $PORT \
  --set-secrets=PROJECT_ID=project_id:latest \
  --service-account=$SERVICE_ACCOUNT \
  --timeout=3600