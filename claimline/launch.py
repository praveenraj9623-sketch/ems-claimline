"""Run the ClaimLine server inside the Streamlit process so the UI and the phone webhooks share one app."""

import threading

from .config import Settings
from .server import Service, create_server


def start_background(settings: Settings) -> tuple[Service, str]:
    """Start the HTTP server (Twilio webhooks + microphone API) on a daemon thread.

    Returns (service, problem). `problem` is a human-readable message if the port is taken; the typed chat
    still works in that case because the UI talks to the service in-process.
    """
    try:
        server, service = create_server(settings)
    except OSError:
        return Service(settings), (
            f"Port {settings.port} is already in use, so phone calls and the microphone call are unavailable. "
            "Close any other ClaimLine window (or change PORT in .env) and restart.")
    threading.Thread(target=server.serve_forever, name="claimline-http", daemon=True).start()
    return service, ""
