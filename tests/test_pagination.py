import time
from zoho_dmarc.collector import Collector
from zoho_dmarc.storage import Writer
from test_collection import CONFIG


def test_changing_inventory_does_not_claim_completion(tmp_path):
    class Changed:
        _check_deadline = lambda self: None
        validate_owner = lambda self: None
        attachments = lambda self, message: []
        passes = 0
        def messages(self, start):
            if start == 1:
                self.passes += 1
                return [{'messageId': str(self.passes), 'receivedTime': str(int(time.time()*1000))}]
            return []
    writer = Writer(tmp_path/'history.sqlite')
    try:
        Collector(CONFIG, writer, Changed()).scan('backfill')
        row = writer.db.execute('SELECT state,reason FROM collection_runs').fetchone()
        assert tuple(row) == ('incomplete', 'unstable_pagination')
        assert not writer.db.execute("SELECT 1 FROM checkpoints WHERE key='last_full_scan'").fetchone()
    finally:
        writer.close()
