"""First administrator provisioning before making a cloud console reachable."""
import argparse
import getpass
import re

from .store import Store, password_hash, uid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="data")
    args = parser.parse_args()
    store = Store(args.data)
    try:
        if store.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            raise SystemExit("An account already exists; use Settings to manage users")
        username = input("Administrator username: ").strip()
        if not re.fullmatch(r"[a-zA-Z0-9_.@-]{3,64}", username):
            raise SystemExit("Use 3..64 letters, numbers, underscores, periods, @ or hyphens")
        password = getpass.getpass("Password (12+ characters): ")
        if len(password) < 12 or len(password) > 256 or password != getpass.getpass("Confirm password: "):
            raise SystemExit("Password length or confirmation is invalid")
        store.db.execute("INSERT INTO users VALUES(?,?,?,?)", (uid("user"), username, password_hash(password), "admin"))
        store.db.commit()
        print("Administrator created. Credentials were not saved in configuration files.")
    finally:
        store.db.close()


if __name__ == "__main__":
    main()
