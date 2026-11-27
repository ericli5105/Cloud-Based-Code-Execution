# Config file for the application.

import os

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "5000"))
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")


SUPPORTED_LANGUAGES = {
    "python": {
        "name": "Python",
        "image": os.getenv("PYTHON_IMAGE", "code-execution-python:latest"),
        "command": ["python", "-I", "-B", "-u", "-c"],
    },
    "javascript": {
        "name": "JavaScript",
        "image": os.getenv("JAVASCRIPT_IMAGE", "code-execution-javascript:latest"),
        "command": ["node", "--eval"],
    },
}


EXECUTION_TIMEOUT = 5  # Seconds, starting immediately before container start.
DOCKER_API_TIMEOUT = 2  # Seconds per Docker API request, including cleanup.
MEMORY_LIMIT = 128 * 1024 * 1024  # Bytes of RAM; additional swap is disabled.
CPU_LIMIT = 0.5  # CPU cores per container.
MAX_SOURCE_SIZE = 64 * 1024  # UTF-8 source bytes.
MAX_OUTPUT_SIZE = 1024 * 1024  # Combined raw stdout/stderr bytes retained.
PROCESS_LIMIT = 64  # Maximum processes/threads per container.
TEMP_STORAGE_SIZE = 16 * 1024 * 1024  # Bytes of writable, memory-backed /tmp.
SHARED_MEMORY_SIZE = 1024 * 1024  # Bytes available in /dev/shm.
MAX_CONCURRENT_EXECUTIONS = 4  # Per backend process; no waiting queue.
# Request bytes: allow JSON escaping overhead while bounding parsing memory.
MAX_CONTENT_LENGTH = MAX_SOURCE_SIZE * 6 + 4096
