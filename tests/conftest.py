import sys
from pathlib import Path

ai_assistant_dir = Path(__file__).resolve().parent.parent / "ai_assistant"
if str(ai_assistant_dir) not in sys.path:
    sys.path.insert(0, str(ai_assistant_dir))
