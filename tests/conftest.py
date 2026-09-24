import os
from pathlib import Path

os.environ.setdefault("POSTGRES_DSN", "postgresql://rec:rec@localhost:55432/rec")
os.environ.setdefault("REDIS_URL", "redis://localhost:56379/1")
os.environ.setdefault("DATA_DIR", str(Path(__file__).resolve().parents[1] / "data"))
