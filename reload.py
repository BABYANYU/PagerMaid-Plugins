"""本地插件刷新：覆盖文件后重载插件，无需重启服务"""
import ast
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from pagermaid.services import scheduler
from pagermaid.static import working_dir

ROOT = Path(working_dir)
BRIDGE = ROOT / '.maintenance/local-plugin-refresh'
BRIDGE.mkdir(mode=0o700, parents=True, exist_ok=True)
LOCK = globals().get('LOCK') or asyncio.Lock()


def write_status(name, payload):
    destination = BRIDGE / name
    temp = destination.with_suffix('.tmp')
    temp.write_text(json.dumps(payload), encoding='utf-8')
    os.chmod(temp, 0o600)
    os.replace(temp, destination)


def validate_request(payload):
    if not isinstance(payload, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(payload.get('id', ''))):
        raise ValueError('Invalid request id')
    plugins = payload.get('plugins')
    if not isinstance(plugins, dict) or not 1 <= len(plugins) <= 20:
        raise ValueError('Invalid plugin list')
    for name, expected_hash in plugins.items():
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', name):
            raise ValueError('Invalid plugin name')
        path = ROOT / 'plugins' / (name + '.py')
        if path.is_symlink() or not path.is_file():
            raise ValueError('Plugin file missing or symlinked')
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != expected_hash:
            raise ValueError('Plugin changed since request')
        ast.parse(source)
    return plugins


@scheduler.scheduled_job('interval', seconds=3, id='codex.local_plugin_refresh',
                         max_instances=1, coalesce=True)
async def refresh_requested_plugins():
    request_path = BRIDGE / 'request.json'
    if LOCK.locked() or not request_path.exists():
        return
    async with LOCK:
        claim = BRIDGE / 'processing.json'
        try:
            os.replace(request_path, claim)
        except FileNotFoundError:
            return
        request_id = None
        try:
            payload = json.loads(claim.read_text(encoding='utf-8'))
            request_id = payload.get('id') if isinstance(payload, dict) else None
            names = validate_request(payload)
            # Do not reset an active API usage query's in-process guard.
            import sys
            api = sys.modules.get('plugins.API')
            if api is not None and getattr(api, 'RUNNING', False):
                raise RuntimeError('API query is running; retry refresh later')
            from pagermaid.common.reload import reload_all
            await reload_all()
            import pagermaid.modules
            missing = set(names) - set(pagermaid.modules.plugin_list)
            if missing:
                raise RuntimeError('Requested plugin failed to load')
            validate_request(payload)
            write_status('result.json', dict(id=request_id, ok=True, plugins=list(names), ts=int(time.time())))
        except Exception as exc:
            # Avoid exposing arbitrary exception text or configuration secrets.
            write_status('result.json', dict(id=request_id, ok=False, error=type(exc).__name__, ts=int(time.time())))
        finally:
            claim.unlink(missing_ok=True)


write_status('ready.json', dict(pid=os.getpid(), loaded_at=int(time.time()), protocol=1))
