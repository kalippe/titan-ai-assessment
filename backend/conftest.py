import os

# Set DATABASE_URL before any app module is imported. database.py creates the
# async engine at import time via os.getenv(), so this must run first.
# Tests connect to the published host port; the api container uses the Docker-
# internal hostname (db:5432) set in .env.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://titan:titan@localhost:5432/titan",
)
