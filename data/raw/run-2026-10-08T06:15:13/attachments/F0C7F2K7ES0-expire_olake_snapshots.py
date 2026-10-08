"""
Expire Iceberg snapshots for every OLake table in a namespace.

Catalog connection is taken from the Spark session (pass --conf flags to
spark-submit, or run inside a Spark environment that already has the
catalog configured). The script only needs the Spark catalog alias.

Examples
  # see what would happen
  spark-submit ... expire_olake_snapshots.py --catalog cat --namespace mydb --dry-run

  # expire, keep newest 50 snapshots per table
  spark-submit ... expire_olake_snapshots.py --catalog cat --namespace mydb --retain-last 50

  # only tables written by OLake (have the olake_2pc table property)
  spark-submit ... expire_olake_snapshots.py --catalog cat --namespace mydb --only-olake

  # explicit list
  spark-submit ... expire_olake_snapshots.py --catalog cat --namespace mydb --tables orders,customers
"""
import argparse
import sys
from datetime import datetime, timezone

from pyspark.sql import SparkSession

ap = argparse.ArgumentParser()
ap.add_argument("--catalog", required=True, help="Spark catalog alias, e.g. cat")
ap.add_argument("--namespace", required=True, help="Iceberg namespace / database")
ap.add_argument("--tables", default="", help="comma-separated allowlist; default = all tables in namespace")
ap.add_argument("--only-olake", action="store_true", help="only tables having the olake_2pc table property")
ap.add_argument("--retain-last", type=int, default=50, help="newest snapshots to keep per table")
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

if args.retain_last < 1:
    sys.exit("--retain-last must be >= 1")

C, NS, KEEP = args.catalog, args.namespace, args.retain_last

spark = SparkSession.builder.appName("olake-expire-snapshots").getOrCreate()
spark.conf.set("spark.sql.session.timeZone", "UTC")
spark.sparkContext.setLogLevel("ERROR")


def q(ident):
    return "`" + ident.replace("`", "``") + "`"


def fqn(t):
    return f"{q(C)}.{q(NS)}.{q(t)}"


def snapshot_count(t):
    return spark.sql(f"SELECT count(*) FROM {fqn(t)}.snapshots").collect()[0][0]


def newest_metadata_file(t):
    rows = spark.sql(
        f"SELECT file FROM {fqn(t)}.metadata_log_entries ORDER BY timestamp DESC LIMIT 1"
    ).collect()
    return rows[0][0] if rows else "?"


def is_olake_table(t):
    props = spark.sql(f"SHOW TBLPROPERTIES {fqn(t)}").collect()
    return any(r["key"] == "olake_2pc" for r in props)


# ---- discover ------------------------------------------------------------
all_tables = [r["tableName"] for r in spark.sql(f"SHOW TABLES IN {q(C)}.{q(NS)}").collect()]
print(f"\nTables in {C}.{NS}: {len(all_tables)}")

if args.tables:
    wanted = [x.strip() for x in args.tables.split(",") if x.strip()]
    missing = sorted(set(wanted) - set(all_tables))
    if missing:
        print(f"WARNING not found: {missing}")
    targets = [t for t in all_tables if t in wanted]
else:
    targets = all_tables

if args.only_olake:
    kept = []
    for t in targets:
        try:
            ok = is_olake_table(t)
        except Exception as e:  # table unreadable -> report, skip
            print(f"  [ERR ] {t}: {e}")
            continue
        print(f"  [{'OLAKE' if ok else 'skip '}] {t}")
        if ok:
            kept.append(t)
    targets = kept

print(f"\nSelected {len(targets)} table(s); retain_last={KEEP}; dry_run={args.dry_run}\n")

# ---- expire --------------------------------------------------------------
done, skipped, failed = [], [], []
for t in targets:
    print("=" * 72)
    print(f"TABLE {NS}.{t}")
    try:
        before = snapshot_count(t)
        print(f"  snapshots before : {before}")
        if before <= KEEP:
            print(f"  skip: already <= {KEEP}")
            skipped.append(t)
            continue
        if args.dry_run:
            print(f"  dry-run: would expire ~{before - KEEP}")
            continue

        cutoff = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        tbl_arg = f"{NS}.{t}".replace("'", "''")
        res = spark.sql(
            f"CALL {q(C)}.system.expire_snapshots("
            f"table => '{tbl_arg}', "
            f"older_than => TIMESTAMP '{cutoff}', "
            f"retain_last => {KEEP}, "
            f"max_concurrent_deletes => 8)"
        ).collect()[0].asDict()
        after = snapshot_count(t)
        print(f"  snapshots after  : {after}")
        print(f"  deleted          : {res}")
        print(f"  newest metadata  : {newest_metadata_file(t)}")
        done.append(t)
    except Exception as e:
        print(f"  FAILED: {e}")
        failed.append((t, str(e).splitlines()[0]))

print("=" * 72)
print(f"\nexpired={len(done)} skipped={len(skipped)} failed={len(failed)}")
for t, err in failed:
    print(f"  FAILED {t}: {err}")

spark.stop()
if failed:
    sys.exit(1)
