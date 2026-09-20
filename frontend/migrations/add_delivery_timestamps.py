"""Add nullable delivery timestamps once, without changing existing order data.

Run from frontend: python -B migrations/add_delivery_timestamps.py --apply
The database config is read without importing/running the Flask application.
"""
import argparse
import ast
from pathlib import Path

import mysql.connector


def load_database_config():
    tree = ast.parse((Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8-sig"))
    node = next(n.value for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "DATABASE_CONFIG" for t in n.targets))
    return {item.arg: ast.literal_eval(item.value) for item in node.keywords}


def migrate(connection):
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'")
        columns = {row[0] for row in cursor.fetchall()}
        if not columns:
            raise RuntimeError("The orders table must exist before running this migration.")
        missing = [name for name in ("picked_up_at", "in_transit_at", "delivered_at") if name not in columns]
        if missing:
            cursor.execute("ALTER TABLE orders " + ", ".join("ADD COLUMN " + name + " TIMESTAMP NULL DEFAULT NULL" for name in missing))
        return missing
    finally:
        cursor.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.parse_args()
    connection = mysql.connector.connect(**load_database_config())
    try:
        added = migrate(connection)
        print("Added: " + ", ".join(added) if added else "Delivery timestamps already installed.")
    finally:
        connection.close()
