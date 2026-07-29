# Use an official, secure, and lightweight Python image
FROM python:3.12-slim

# Prevent Python from writing temporary .pyc files to the cloud disk
ENV PYTHONDONTWRITEBYTECODE=1
# Ensure print statements and errors show up immediately in your Google Cloud mobile app logs
ENV PYTHONUNBUFFERED=1
# Force ONNX Runtime to use CPU execution provider path
ENV ONNX_CPU_ONLY=1

# Set the working folder inside the server container
WORKDIR /app

# Install system utilities (specifically FFmpeg, which your pipeline uses to find music beats)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy your Python dependencies file and install them instantly
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your local repository scripts into the cloud image
COPY . .

# Create a secure non-root user and group, and transfer directory ownership
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -d /app -s /sbin/nologin appuser && \
    chown -R appuser:appgroup /app

# Switch to the non-root user
USER appuser

# Expose port 8080 (the exact entry point Google Cloud Run expects)
EXPOSE 8080

# Kick off the Flask application when the server boots up
CMD ["python", "run_studio.py"]
