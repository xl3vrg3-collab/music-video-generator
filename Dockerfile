FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create output directories
RUN mkdir -p output/prompt_os/photos/characters \
    output/prompt_os/photos/costumes \
    output/prompt_os/photos/environments \
    output/prompt_os/photos/props \
    output/prompt_os/sheets \
    output/first_frames \
    output/scene_thumbnails \
    output/auto_director/clips \
    output/audio \
    output/reference_demos \
    output/templates \
    output/director_brain \
    output/autoagent \
    output/album_art \
    output/stems \
    output/uploads \
    output/projects

# Port from environment variable (Railway sets this)
ENV PORT=3849

EXPOSE ${PORT}

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

# Run server
CMD ["python", "-B", "server.py"]
