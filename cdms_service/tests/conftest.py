"""Pytest fixtures and environment configuration for tests."""

import os

# Set testing environment variables before any application imports
os.environ["DATABASE_URL"] = "sqlite:///./test_cdms.db"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DEBUG"] = "true"
