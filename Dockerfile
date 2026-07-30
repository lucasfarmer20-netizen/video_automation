# --- STAGE 1: Build the Next.js static frontend ---
FROM node:20-alpine AS builder
WORKDIR /app

# Copy dependency specifications and install packages
COPY frontend/package*.json ./
RUN npm ci

# Copy the rest of the frontend source code and compile static files
COPY frontend/ ./
RUN npm run build

# --- STAGE 2: Compile the FastAPI backend and final deployment runtime ---
FROM python:3.12-slim
WORKDIR /app

# Prevent Python from writing temporary .pyc files to the cloud disk
ENV PYTHONDONTWRITEBYTECODE=1
# Ensure print statements and errors show up immediately in Cloud Logging
ENV PYTHONUNBUFFERED=1
# Force ONNX Runtime to use CPU execution provider path
ENV ONNX_CPU_ONLY=1

# Install system utilities (FFmpeg for video/audio compilation)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy backend requirements and install python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all python modules and scripts
COPY . .

# Copy Next.js built static assets from the builder stage
COPY --from=builder /app/out ./frontend/out

# Create a secure non-root user and group, and transfer directory ownership
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -d /app -s /sbin/nologin appuser && \
    chown -R appuser:appgroup /app

# Switch to the non-root user
USER appuser

# Expose port 8080 (Cloud Run expected port)
EXPOSE 8080

# Launch uvicorn to serve the API and static frontend UI
CMD ["python", "run_studio.py"]
