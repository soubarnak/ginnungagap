"""Spaces test suite."""
import os

# Existing tests assert systemd commands; pin the backend unless overridden.
os.environ.setdefault("SPACES_HOST_BACKEND", "systemd")
