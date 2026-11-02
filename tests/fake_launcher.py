"""Acts like `lms server start`: spawns the fake server detached, writes its pid, exits 0.
python tests/fake_launcher.py --port P --reply-file F --pid-file PF
With --stop it instead kills the pid in the pid file (the matching stop command)."""
import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

FAKE = str(Path(__file__).resolve().parent / "fake_openai_server.py")

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int)
ap.add_argument("--reply-file")
ap.add_argument("--pid-file", required=True)
ap.add_argument("--stop", action="store_true")
args = ap.parse_args()
pid_file = Path(args.pid_file)

if args.stop:
    os.kill(int(pid_file.read_text()), signal.SIGTERM)
    sys.exit(0)

kw = ({"start_new_session": True} if sys.platform != "win32" else
      {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP})
child = subprocess.Popen([sys.executable, FAKE, "--port", str(args.port), "--reply-file", args.reply_file],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
pid_file.write_text(str(child.pid))
sys.exit(0)
