"""
User management CLI for the Funding Rate Detector.

Usage:
    python manage_users.py add <login> <password>    — Add a new user
    python manage_users.py remove <login>             — Remove a user
    python manage_users.py list                       — List all users (logins only)
    python manage_users.py change <login> <password>  — Change password
"""
import sys
import json
import os

USERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")


def load_users() -> dict:
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_users(users: dict):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=4, ensure_ascii=False)
    print(f"[✓] Saved to {USERS_FILE}")


def cmd_add(login: str, password: str):
    users = load_users()
    if login in users:
        print(f"[!] User '{login}' already exists. Use 'change' to update password.")
        sys.exit(1)
    users[login] = password
    save_users(users)
    print(f"[✓] User '{login}' added.")
    print("    ⚠️  Restart the app for changes to take effect.")


def cmd_remove(login: str):
    users = load_users()
    if login not in users:
        print(f"[!] User '{login}' not found.")
        sys.exit(1)
    del users[login]
    save_users(users)
    print(f"[✓] User '{login}' removed.")
    print("    ⚠️  Restart the app for changes to take effect.")


def cmd_change(login: str, password: str):
    users = load_users()
    if login not in users:
        print(f"[!] User '{login}' not found.")
        sys.exit(1)
    users[login] = password
    save_users(users)
    print(f"[✓] Password for '{login}' updated.")
    print("    ⚠️  Restart the app for changes to take effect.")


def cmd_list():
    users = load_users()
    if not users:
        print("[*] No users configured.")
        return
    print(f"[*] {len(users)} user(s):")
    for login in sorted(users.keys()):
        print(f"    • {login}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "add":
        if len(sys.argv) != 4:
            print("Usage: python manage_users.py add <login> <password>")
            sys.exit(1)
        cmd_add(sys.argv[2], sys.argv[3])

    elif cmd == "remove":
        if len(sys.argv) != 3:
            print("Usage: python manage_users.py remove <login>")
            sys.exit(1)
        cmd_remove(sys.argv[2])

    elif cmd == "change":
        if len(sys.argv) != 4:
            print("Usage: python manage_users.py change <login> <password>")
            sys.exit(1)
        cmd_change(sys.argv[2], sys.argv[3])

    elif cmd == "list":
        cmd_list()

    else:
        print(f"[!] Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
