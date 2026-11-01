import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


def now():
    return time.time()


def uid(prefix):
    return f"{prefix}_{secrets.token_hex(6)}"


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"{salt}:{digest}"


def check_password(password, stored):
    salt, _ = stored.split(":", 1)
    return secrets.compare_digest(password_hash(password, salt), stored)


class Store:
    """Small single-site SQLite store. Only the API process writes this database."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        key_path = self.directory / "encryption.key"
        if not key_path.exists():
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())
        self.db = sqlite3.connect(self.directory / "luminext.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO schema_version VALUES(1);
            CREATE TABLE IF NOT EXISTS records(kind TEXT,id TEXT,body TEXT,PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE,password TEXT,role TEXT);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,time REAL,kind TEXT,entity TEXT,actor TEXT,body TEXT);
            CREATE INDEX IF NOT EXISTS event_entity ON events(entity,time);
        """)
        self.db.commit()

    def seal(self, value):
        return self.cipher.encrypt(value.encode()).decode() if value else ""

    def unseal(self, value):
        return self.cipher.decrypt(value.encode()).decode() if value else ""

    def all(self, kind):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM records WHERE kind=? ORDER BY id", (kind,))]

    def get(self, kind, id, default=None):
        r = self.db.execute("SELECT body FROM records WHERE kind=? AND id=?", (kind, id)).fetchone()
        return json.loads(r[0]) if r else default

    def put(self, kind, value):
        self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?)", (kind, value["id"], json.dumps(value)))
        self.db.commit()
        return value

    def delete(self, kind, id):
        self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))
        self.db.commit()

    def event(self, kind, entity, body, actor="system"):
        self.db.execute("INSERT INTO events(time,kind,entity,actor,body) VALUES(?,?,?,?,?)", (now(), kind, entity, actor, json.dumps(body)))
        self.db.commit()

    def history(self, entity=None, limit=150):
        query = "SELECT * FROM events" + (" WHERE entity=?" if entity else "") + " ORDER BY id DESC LIMIT ?"
        args = (entity, limit) if entity else (limit,)
        return [{**dict(r), "body": json.loads(r["body"])} for r in self.db.execute(query, args)]

    def prune(self, days=30):
        self.db.execute("DELETE FROM events WHERE time<?", (now() - days * 86400,))
        self.db.execute("DELETE FROM sessions WHERE expires<?", (now(),))
        self.db.commit()

    def backup(self):
        folder = self.directory / "backups"
        folder.mkdir(exist_ok=True)
        path = folder / f"luminext-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.db"
        target = sqlite3.connect(path)
        self.db.backup(target)
        target.close()
        for old in sorted(folder.glob("*.db"))[:-7]:
            old.unlink()
        return path
