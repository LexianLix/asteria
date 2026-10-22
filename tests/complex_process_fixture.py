"""Owned parent + child for cancellation, never imported by the application."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
args, _ = parser.parse_known_args()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
args.output.with_suffix('.pid').write_text(json.dumps({'parent': __import__('os').getpid(), 'child': child.pid}), encoding='utf-8')
time.sleep(60)
