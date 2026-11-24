import sys
from pathlib import Path

# Make `agent` importable when pytest is invoked from anywhere in the repo.
sys.path.insert(0, str(Path(__file__).resolve().parent))
