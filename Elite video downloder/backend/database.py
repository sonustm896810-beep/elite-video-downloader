"""
database.py — SQLite database layer for Elite Video Downloader
Handles user registration storage with hashed passwords.
"""

import sqlite3
import os
import bcrypt as _bcrypt

# Database file lives alongside this script
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elite_downloader.db")


def _hash_password(password: str) -> str:
    """Hash a password with bcrypt, truncating to 72 bytes if needed."""
    pw_bytes = password.encode("utf-8")[:72]
    return _bcrypt.hashpw(pw_bytes, _bcrypt.gensalt()).decode("utf-8")


def _check_password(password: str, hashed: str) -> bool:
    """Verify a password against a bcrypt hash."""
    pw_bytes = password.encode("utf-8")[:72]
    return _bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))


def get_connection():
    """Create and return a new SQLite connection with row-factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the users table if it doesn't already exist."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            email       TEXT    NOT NULL UNIQUE,
            password    TEXT    NOT NULL,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print(f"[OK] Database initialized at {DB_PATH}")


# --------------- User CRUD helpers ---------------

def create_user(name: str, email: str, password: str) -> dict:
    """
    Register a new user.
    Returns the user dict on success, raises ValueError on duplicate email.
    """
    hashed = _hash_password(password)
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
            (name, email, hashed),
        )
        conn.commit()
        user_id = cursor.lastrowid
        return {"id": user_id, "name": name, "email": email}
    except sqlite3.IntegrityError:
        raise ValueError("An account with this email already exists.")
    finally:
        conn.close()


def verify_user(email: str, password: str) -> dict | None:
    """
    Verify login credentials.
    Returns user dict if valid, None otherwise.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()

    if row and _check_password(password, row["password"]):
        return {"id": row["id"], "name": row["name"], "email": row["email"]}
    return None


def get_user_by_email(email: str) -> dict | None:
    """Look up a user by email (without password verification)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name, email, created_at FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None
