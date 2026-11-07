"""Load the c360 kit CSVs into Aurora Postgres and/or Redshift.

Usage:
    C360_DATA=<path to knowledge-kits>/examples/c360/data python load.py all   # or: aurora | redshift

Needs: pip install psycopg2-binary redshift_connector python-dotenv
Reads connection settings from .env.aws, which 02_aurora.sh and 03_redshift.sh write.
The CSVs come from Stardog's knowledge-kits repo (examples/c360/data).
"""

import csv
import io
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("C360_DATA", HERE.parent / "knowledge-kits" / "examples" / "c360" / "data"))
SQL = HERE / "sql"

# (csv file, target table, columns) — order matters for FKs in Aurora.
AURORA = [
    ("US_Locations.csv", "c360.address", ["id", "street_number", "street_name", "street_suffix", "state", "zip", "city"]),
    ("US_Customers.csv", "c360.customer", ["cid", "first_name", "last_name", "email", "ssn", "picture", "phone", "location", "full_name"]),
    ("CreditCards.csv", "c360.credit_card", ["id", "cid", "card_num", "card_type"]),
    ("Rewards.csv", "c360.rewards_account", ["id", "cid", "account_id", "create_date"]),
]
REDSHIFT = [
    ("Vendors.csv", "c360.vendor", ["id", "vendor_name", "industry", "homepage"]),
    ("category.csv", "c360.category", ["id", "dept_name", "parent", "description"]),
    ("product_catalog.csv", "c360.product", ["id", "name", "brand", "price", "dept", "description"]),
    ("Purchases.csv", "c360.purchase", ["id", "pid", "date", "time", "quantity", "price", "cid", "card", "reward_account"]),
]


def read_rows(name, cols):
    raw = (DATA / name).read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # Vendors.csv has a stray cp1252 byte
        text = raw.decode("cp1252")
    rows = csv.DictReader(io.StringIO(text))
    return [tuple((r[c] or None) for c in cols) for r in rows]


def run_ddl(cur, path):
    for stmt in path.read_text().split(";"):
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        if body:
            cur.execute(body)


def load(conn, ddl, tables, batch):
    cur = conn.cursor()
    run_ddl(cur, ddl)
    for fname, table, cols in tables:
        rows = read_rows(fname, cols)
        ph = "(" + ",".join(["%s"] * len(cols)) + ")"
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES " + ",".join([ph] * len(chunk))
            cur.execute(sql, [v for row in chunk for v in row])
        conn.commit()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        print(f"  {table:24s} {cur.fetchone()[0]:>6} rows  (csv: {len(rows)})")


def aurora():
    import psycopg2

    conn = psycopg2.connect(
        host=os.environ["AURORA_HOST"], port=os.environ.get("AURORA_PORT", 5432),
        dbname=os.environ["AURORA_DB"], user=os.environ["AURORA_USER"],
        password=os.environ["AURORA_PASSWORD"], sslmode="require",
    )
    print("Aurora:")
    load(conn, SQL / "aurora_schema.sql", AURORA, batch=500)


def redshift():
    import redshift_connector

    conn = redshift_connector.connect(
        host=os.environ["REDSHIFT_HOST"], port=int(os.environ.get("REDSHIFT_PORT", 5439)),
        database=os.environ["REDSHIFT_DB"], user=os.environ["REDSHIFT_USER"],
        password=os.environ["REDSHIFT_PASSWORD"],
    )
    print("Redshift:")
    load(conn, SQL / "redshift_schema.sql", REDSHIFT, batch=250)


if __name__ == "__main__":
    load_dotenv(HERE / ".env.aws")
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target in ("aurora", "all"):
        aurora()
    if target in ("redshift", "all"):
        redshift()
