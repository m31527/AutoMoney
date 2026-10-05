"""Deployment helper: opt in to the separate paid replay endpoint, preserving live settings."""
import argparse
import os
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('openteddy', type=Path)
parser.add_argument('--cloud-model', required=True)
args = parser.parse_args()
if not args.cloud_model or any(c.isspace() for c in args.cloud_model) or '=' in args.cloud_model:
    parser.error('Invalid model name')
p = args.openteddy.expanduser() / '.automoney.env'
if not p.is_file():
    parser.error('Install the OpenTeddy bridge first')
updates = {'AUTOMONEY_REPLAY_ENABLED': 'true', 'AUTOMONEY_REPLAY_ALLOW_OPENAI': 'true',
           'AUTOMONEY_REPLAY_OPENAI_MODEL': args.cloud_model, 'AUTOMONEY_REPLAY_DAILY_CALL_LIMIT': '12'}
lines = [line for line in p.read_text().splitlines() if line.split('=',1)[0] not in updates]
lines += [f'{key}={value}' for key,value in updates.items()]
p.write_text('\n'.join(lines)+'\n')
os.chmod(p, 0o600)
print('Enabled bounded paid replay only. Restart OpenTeddy. Live model settings preserved.')
