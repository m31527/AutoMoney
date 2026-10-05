"""Install the optional router into a local OpenTeddy checkout; preserve secrets and backups."""
import os
import secrets
import shutil
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
target = Path(sys.argv[1] if len(sys.argv) > 1 else '~/OpenTeddy').expanduser().resolve()
main = target / 'main.py'
if not main.is_file() or 'app = FastAPI(' not in main.read_text():
    raise SystemExit('Not a supported OpenTeddy checkout')
for source in (root / 'integrations/openteddy').glob('automoney_*.py'):
    shutil.copy2(source, target / source.name)
settings = target / '.automoney.env'
if not settings.exists():
    fd = os.open(settings, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('AUTOMONEY_TOKEN=' + secrets.token_hex(32) + '\n'
                'AUTOMONEY_PROVIDER=ollama\nAUTOMONEY_MODEL=qwen3.8:27b\n'
                'AUTOMONEY_ALLOW_OPENAI=false\nAUTOMONEY_DAILY_CALL_LIMIT=96\n')
values = dict(line.split('=', 1) for line in settings.read_text().splitlines()
              if '=' in line and not line.startswith('#'))
client = root / 'config/openteddy.env'
if not client.exists():
    fd = os.open(client, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('OPENTEDDY_URL=http://127.0.0.1:8000\n'
                'OPENTEDDY_PROVIDER=' + values['AUTOMONEY_PROVIDER'] + '\n'
                'OPENTEDDY_MODEL=' + values['AUTOMONEY_MODEL'] + '\n'
                'OPENTEDDY_TOKEN=' + values['AUTOMONEY_TOKEN'] + '\n')
client_ignore = root / '.gitignore'
client_rules = client_ignore.read_text() if client_ignore.exists() else ''
if 'config/openteddy.env' not in client_rules.splitlines():
    client_ignore.write_text(client_rules + '\nconfig/openteddy.env\n')
marker = '# AutoMoney isolated research router'
text = main.read_text()
if marker not in text:
    backup = target / 'main.py.before-automoney'
    if not backup.exists():
        shutil.copy2(main, backup)
    main.write_text(text + '\n\n' + marker + '\nfrom automoney_api import router as automoney_router\napp.include_router(automoney_router)\n')
# Keep generated shared secret out of this checkout's commits.
ignore = target / '.gitignore'
old = ignore.read_text() if ignore.exists() else ''
for name in ('.automoney.env', '.automoney-quota.db', '.automoney-replay-quota.db', 'main.py.before-automoney'):
    if name not in old.splitlines():
        old += '\n' + name + '\n'
ignore.write_text(old)
print('Installed. Restart OpenTeddy, check config/openteddy.env URL, then run scripts/openteddy-shadow.sh.')
