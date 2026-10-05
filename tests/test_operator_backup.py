import os
from pathlib import Path
import subprocess
import sys
import json
from zoho_dmarc.storage import Writer, reader
from zoho_dmarc.parser import parse
from test_reports import XML, enqueue


def test_live_operator_backup_and_restore(tmp_path):
    path = tmp_path/'live.sqlite'
    writer = Writer(path)
    writer.ingest(enqueue(writer), parse(XML, ('example.test',)))
    root = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run([sys.executable,'-m','zoho_dmarc','backup'], cwd=root, env={**os.environ,'DMARC_DATABASE':str(path)}, capture_output=True, check=True)
        backup = tmp_path/'backups'/json.loads(result.stdout)['file']
        restored = tmp_path/'restored.sqlite'
        subprocess.run([sys.executable,'-m','zoho_dmarc','restore','--source',str(backup),'--target',str(restored)], cwd=root, check=True)
        with reader(restored) as db:
            assert db.execute('SELECT SUM(count) FROM aggregate_rows').fetchone()[0]==12
            assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        backup.with_suffix('.sqlite.sha256').write_text('0'*64)
        tampered = subprocess.run([sys.executable,'-m','zoho_dmarc','restore','--source',str(backup),'--target',str(tmp_path/'bad.sqlite')], cwd=root, capture_output=True)
        assert tampered.returncode!=0
        assert not (tmp_path/'bad.sqlite').exists()
    finally:
        writer.close()
