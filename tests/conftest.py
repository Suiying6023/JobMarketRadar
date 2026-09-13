"""Tests use explicit fixture preferences, never the workstation's private .env."""
import os

# Legacy script tests were written for the documented three-city fixture.
# Individual preference tests can still override this with monkeypatch.
os.environ["JOB_RADAR_CITIES"] = "杭州,上海,深圳"
