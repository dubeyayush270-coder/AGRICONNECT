from __future__ import annotations
import argparse, ast
from pathlib import Path
import mysql.connector

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"

def load_database_config():
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "DATABASE_CONFIG" for t in node.targets):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "dict":
                return {kw.arg: ast.literal_eval(kw.value) for kw in value.keywords if kw.arg}
            if isinstance(value, ast.Dict):
                return ast.literal_eval(value)
    raise RuntimeError("DATABASE_CONFIG not found in app.py")

def exists(cursor, sql, params):
    cursor.execute(sql, params)
    return cursor.fetchone() is not None

def column_exists(cursor, table, column):
    return exists(cursor, """SELECT 1 FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND COLUMN_NAME=%s LIMIT 1""", (table, column))

def index_exists(cursor, table, name):
    return exists(cursor, """SELECT 1 FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s AND INDEX_NAME=%s LIMIT 1""", (table, name))

def constraint_exists(cursor, name):
    return exists(cursor, """SELECT 1 FROM information_schema.TABLE_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA=DATABASE() AND CONSTRAINT_NAME=%s LIMIT 1""", (name,))

def apply_migration(connection):
    c = connection.cursor()
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS logistics_routes (
            route_id INT NOT NULL AUTO_INCREMENT,
            logistics_id INT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
            current_load_kg DECIMAL(12,2) NOT NULL DEFAULT 0.00,
            planned_distance_km DECIMAL(12,3) NOT NULL DEFAULT 0.000,
            route_version INT NOT NULL DEFAULT 1,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP NULL DEFAULT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            completed_at TIMESTAMP NULL DEFAULT NULL,
            PRIMARY KEY(route_id),
            INDEX idx_logistics_routes_driver_status(logistics_id,status),
            CONSTRAINT fk_logistics_routes_driver FOREIGN KEY(logistics_id)
                REFERENCES logistics_profiles(logistics_id) ON DELETE CASCADE
        )""")

        c.execute("""CREATE TABLE IF NOT EXISTS logistics_route_stops (
            stop_id INT NOT NULL AUTO_INCREMENT,
            route_id INT NOT NULL,
            order_id INT NOT NULL,
            stop_type VARCHAR(20) NOT NULL,
            sequence_no INT NOT NULL,
            latitude DECIMAL(10,7) NOT NULL,
            longitude DECIMAL(10,7) NOT NULL,
            address TEXT NULL,
            quantity_delta DECIMAL(12,2) NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'PLANNED',
            planned_eta DATETIME NULL,
            actual_arrival_at DATETIME NULL,
            completed_at DATETIME NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(stop_id),
            UNIQUE KEY uq_route_order_stop(route_id,order_id,stop_type),
            INDEX idx_route_stops_sequence(route_id,sequence_no),
            INDEX idx_route_stops_status(route_id,status),
            CONSTRAINT fk_route_stops_route FOREIGN KEY(route_id)
                REFERENCES logistics_routes(route_id) ON DELETE CASCADE,
            CONSTRAINT fk_route_stops_order FOREIGN KEY(order_id)
                REFERENCES orders(order_id) ON DELETE CASCADE
        )""")

        if not column_exists(c, "orders", "logistics_route_id"):
            c.execute("ALTER TABLE orders ADD COLUMN logistics_route_id INT NULL AFTER assigned_logistics_id")
        if not index_exists(c, "orders", "idx_orders_logistics_route"):
            c.execute("ALTER TABLE orders ADD INDEX idx_orders_logistics_route(logistics_route_id)")
        if not constraint_exists(c, "fk_orders_logistics_route"):
            c.execute("""ALTER TABLE orders ADD CONSTRAINT fk_orders_logistics_route
                FOREIGN KEY(logistics_route_id) REFERENCES logistics_routes(route_id) ON DELETE SET NULL""")

        request_columns = {
            "route_fit_type": "VARCHAR(30) NULL",
            "pickup_deviation_km": "DECIMAL(10,3) NULL",
            "added_distance_km": "DECIMAL(10,3) NULL",
            "added_distance_pct": "DECIMAL(8,3) NULL",
            "suggested_route_id": "INT NULL",
            "suggested_pickup_sequence": "INT NULL",
            "suggested_delivery_sequence": "INT NULL",
            "route_evaluated_at": "TIMESTAMP NULL DEFAULT NULL",
        }
        for name, definition in request_columns.items():
            if not column_exists(c, "logistics_order_requests", name):
                c.execute(f"ALTER TABLE logistics_order_requests ADD COLUMN {name} {definition}")

        if not index_exists(c, "logistics_order_requests", "idx_lor_suggested_route"):
            c.execute("ALTER TABLE logistics_order_requests ADD INDEX idx_lor_suggested_route(suggested_route_id)")

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        c.close()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not args.apply:
        print("Dry run only. Re-run with --apply to change the database.")
        return
    connection = mysql.connector.connect(**load_database_config())
    try:
        apply_migration(connection)
    finally:
        connection.close()
    print("Multi-order logistics migration applied successfully.")

if __name__ == "__main__":
    main()
