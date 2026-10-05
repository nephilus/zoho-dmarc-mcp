import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
from .config import database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("collector", "server", "backup", "restore", "reconcile", "backup-list", "probe"))
    parser.add_argument("--source")
    parser.add_argument("--target")
    args = parser.parse_args()
    if args.command == "collector":
        from .collector import run
        run()
    elif args.command == "server":
        from .server import run
        run()
    elif args.command == "probe":
        import time
        heartbeat = Path(database()).parent / "collector-heartbeat"
        raise SystemExit(0 if heartbeat.exists() and time.time()-int(heartbeat.read_text()) < 7500 else 1)
    elif args.command == "backup-list":
        print(json.dumps([{"file": path.name, "sha256": path.with_suffix(path.suffix+".sha256").read_text().strip()} for path in sorted((Path(database()).parent/"backups").glob("*.sqlite"))]))
    elif args.command == "backup":
        from .storage import reader, create_backup
        with reader(database()) as source:
            target = create_backup(source, Path(database()).parent/'backups')
        print(json.dumps({'file': target.name}))
    elif args.command == "restore":
        if not args.source or not args.target or Path(args.target).exists():
            parser.error("restore needs source and a new target")
        source = Path(args.source)
        expected = source.with_suffix(source.suffix + ".sha256").read_text().strip()
        from .storage import file_hash
        if file_hash(source) != expected:
            raise RuntimeError("backup_hash_mismatch")
        from .storage import reader
        with reader(source) as original, sqlite3.connect(args.target) as target:
            if original.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("backup_integrity_failed")
            original.backup(target)
    else:
        from .storage import Writer
        writer = Writer(database())
        try:
            from .config import Config
            from .collector import Collector
            from .zoho import Zoho
            config = Config.load()
            Collector(config, writer, Zoho(config)).scan("reconcile")
        finally:
            writer.close()


if __name__ == "__main__":
    main()
