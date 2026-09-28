"""ASGI entrypoint: `uvicorn jarvis.api.main:app`."""
from jarvis.api.app import create_app

app = create_app()
