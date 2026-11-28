import os
import tempfile

# Tests import `app`, which initializes the state DB/session store. Keep that
# out of /var/lib and never start background threads during a test run.
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="agentcontrol-test-"))
os.environ.setdefault("PANEL_DISABLE_BACKGROUND", "1")
