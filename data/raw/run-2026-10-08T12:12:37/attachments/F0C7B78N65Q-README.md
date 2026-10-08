# OLake → BigLake: fixing the "metadata.json over 1 MB" sync failure

**Who this is for:** anyone running OLake with the BigLake (Google Cloud Lakehouse) Iceberg REST catalog as the destination, whose syncs have started failing because a table's Iceberg `metadata.json` exceeded 1 MB.

**What you will do:** expire old Iceberg snapshots on every affected table with Spark, which shrinks `metadata.json` immediately, then resume OLake. Table data is not touched. Expected time: 15 to 30 minutes.

---

## 1. What is happening

- BigLake enforces a hard limit: *"The Apache Iceberg `metadata.json` file size is limited to 1 MB ... If your metadata file exceeds this size, you might encounter errors when performing table operations through the Apache Iceberg REST catalog endpoint."* ([Google troubleshooting doc](https://docs.cloud.google.com/lakehouse/docs/troubleshooting))
- Every OLake commit adds one snapshot entry to `metadata.json`. OLake does not expire snapshots on its own, so the file grows with every sync batch until BigLake rejects the commit. CDC syncs with frequent commits hit the limit fastest.
- BigQuery SQL has no statement to expire snapshots on a REST-catalog table. The only lever is Iceberg's own `expire_snapshots` maintenance procedure, run from Spark (or any engine that supports it) through the same BigLake REST catalog.
- Expiring snapshots removes old time-travel history and the data files that only those old snapshots referenced. The current table contents, schema, and OLake's `olake_2pc` state property are unaffected. OLake's own checkpoint lives in OLake's state file, not in Iceberg snapshots, so the sync resumes where it left off.

We verified this end to end on 2026-10-07 against a BigLake REST catalog:

| | Before | After `expire_snapshots` |
|---|---|---|
| Snapshots | 10 | 2 |
| `metadata.json` size | 12,243 bytes | 4,851 bytes |
| Rows in table | 10 | 10 |

A second run, using the loop script from section 4, took the same table from 19 snapshots to 5 and `metadata.json` from 24,416 to 11,347 bytes. With thousands of snapshots the reduction is proportionally much larger.

---

## 2. Before you start

1. **Pause every OLake job writing to this catalog.** Spark and OLake must not commit to the same table at the same time.
2. **Pick how many snapshots to keep per table** (`retain_last`). We recommend **50** for the emergency run. Rough math: if a table hit 1 MB at *N* snapshots, keeping 50 leaves roughly `50 / N` MB. Keeping 50 also preserves a short time-travel window. Use a larger number only if you need more history and have the headroom.
3. **Know your catalog identifiers.** From the OLake destination config:
   - `rest_catalog_url` is always `https://biglake.googleapis.com/iceberg/v1/restcatalog`
   - `iceberg_s3_path` is either `gs://<bucket>` (single-bucket catalog, catalog id = bucket name) or `bl://projects/<project>/catalogs/<catalog-id>` (multi-bucket catalog)
   - `gcp_project_id`
   - the namespace (database) OLake writes into
4. **Decide which tables.** If the namespace holds only OLake tables, run against all of them. If it is mixed, the script below can select only tables that carry OLake's `olake_2pc` table property, or you can pass an explicit list.

---

## 3. Permissions

Whatever identity runs Spark needs:

| Permission | Where | Note |
|---|---|---|
| `roles/biglake.editor` | project | read + commit tables through the REST catalog |
| `roles/storage.objectUser` | the table bucket(s) | only if the catalog is **not** in vended-credentials mode |
| `roles/serviceusage.serviceUsageConsumer` | project | add if you get `PERMISSION_DENIED` mentioning `serviceusage.services.use` |

**Fastest option: reuse the service account OLake already uses.** It already has `biglake.editor` and bucket access, otherwise OLake could not have written the tables. Point Spark at the same key file and no new IAM work is needed.

The catalog's own service account (`blirc-...@gcp-sa-biglakerestcatalog.iam.gserviceaccount.com`) must have `roles/storage.objectUser` on the bucket. If OLake commits were working before the size limit was hit, this is already in place.

Check the catalog's credential mode if unsure:

```bash
gcloud biglake iceberg catalogs describe <catalog-id> --project=<project>
```

---

## 4. The fix

Choose one path.

### Path A: you already have Spark connected to this catalog

Any Spark 3.4+ with the Iceberg runtime and the BigLake catalog configured works: a Dataproc cluster, a notebook, a Spark Connect session, Trino/Flink equivalents also exist but are not covered here.

**Single table, plain SQL:**

```sql
-- replace <alias> with your Spark catalog alias, <db>.<table> with the Iceberg identifier
SELECT count(*) FROM <alias>.<db>.<table>.snapshots;

CALL <alias>.system.expire_snapshots(
  table       => '<db>.<table>',
  older_than  => TIMESTAMP '2099-01-01 00:00:00',   -- "everything older than now"
  retain_last => 50                                 -- but always keep the newest 50
);

SELECT count(*) FROM <alias>.<db>.<table>.snapshots;
```

`older_than` in the future plus `retain_last` means "keep exactly the newest 50, expire the rest". Iceberg never deletes a snapshot that is still needed by a retained one.

**Every table in a namespace, loop script.** Save `expire_olake_snapshots.py` (next to this README) and run it with your existing Spark setup:

```bash
# see what would be done, no changes
spark-submit expire_olake_snapshots.py --catalog <alias> --namespace <db> --dry-run

# expire, keep newest 50 per table
spark-submit expire_olake_snapshots.py --catalog <alias> --namespace <db> --retain-last 50

# mixed namespace: only tables that have OLake's olake_2pc property
spark-submit expire_olake_snapshots.py --catalog <alias> --namespace <db> --only-olake --retain-last 50

# or an explicit list
spark-submit expire_olake_snapshots.py --catalog <alias> --namespace <db> --tables orders,customers --retain-last 50
```

The script prints snapshot counts before and after for each table, continues past failures, and exits non-zero at the end if any table failed. Example output:

```text
TABLE expire_ns.expire_tbl
  snapshots before : 19
  snapshots after  : 5
  deleted          : {'deleted_data_files_count': 17, 'deleted_manifest_files_count': 17, 'deleted_manifest_lists_count': 14, ...}
  newest metadata  : gs://.../metadata/00031-....metadata.json
expired=1 skipped=0 failed=0
```

### Path B: no Spark yet. Run it from a laptop or VM (verified path)

This is what we tested. Needs Java 17 or 21, Python 3.9+, network access to Google APIs, and a GCP identity (section 3).

```bash
# 1. PySpark in a throwaway venv (Spark 4.0 for Java 21; use pyspark==3.5.5 on Java 17)
python3 -m venv sparkenv && ./sparkenv/bin/pip install pyspark==4.0.1

# 2. credentials: EITHER the OLake service-account key ...
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/olake-sa.json
#    ... OR your own user login
gcloud auth application-default login

# 3. catalog identifiers
export PROJECT_ID=<project>
export CATALOG_ID=<catalog-id>      # bucket name for single-bucket catalogs
export ALIAS=cat                    # arbitrary Spark-side name
export ICEBERG=1.11.0
export RUNTIME=iceberg-spark-runtime-4.0_2.13     # Spark 4.0; use iceberg-spark-runtime-3.5_2.12 on Spark 3.5
export SPARK_HOME=$(./sparkenv/bin/python -c "import pyspark,os;print(os.path.dirname(pyspark.__file__))")
export PYSPARK_PYTHON=$PWD/sparkenv/bin/python

# 4. run (first run downloads the Iceberg jars, about 1 minute)
$SPARK_HOME/bin/spark-submit \
  --packages org.apache.iceberg:${RUNTIME}:${ICEBERG},org.apache.iceberg:iceberg-gcp-bundle:${ICEBERG} \
  --conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions \
  --conf spark.sql.defaultCatalog=${ALIAS} \
  --conf spark.sql.catalog.${ALIAS}=org.apache.iceberg.spark.SparkCatalog \
  --conf spark.sql.catalog.${ALIAS}.type=rest \
  --conf spark.sql.catalog.${ALIAS}.uri=https://biglake.googleapis.com/iceberg/v1/restcatalog \
  --conf spark.sql.catalog.${ALIAS}.warehouse=bl://projects/${PROJECT_ID}/catalogs/${CATALOG_ID} \
  --conf spark.sql.catalog.${ALIAS}.io-impl=org.apache.iceberg.gcp.gcs.GCSFileIO \
  --conf spark.sql.catalog.${ALIAS}.header.x-goog-user-project=${PROJECT_ID} \
  --conf spark.sql.catalog.${ALIAS}.rest.auth.type=org.apache.iceberg.gcp.auth.GoogleAuthManager \
  --conf spark.sql.catalog.${ALIAS}.header.X-Iceberg-Access-Delegation=vended-credentials \
  --conf spark.sql.catalog.${ALIAS}.gcs.oauth2.refresh-credentials-endpoint=https://oauth2.googleapis.com/token \
  --conf spark.ui.enabled=false \
  expire_olake_snapshots.py --catalog ${ALIAS} --namespace <db> --dry-run
```

Review the dry-run output, then rerun without `--dry-run` (add `--retain-last 50`, and `--only-olake` or `--tables ...` for a mixed namespace).

Notes:
- `warehouse` must match how the catalog was created. `bl://projects/<project>/catalogs/<id>` works for both single-bucket (id = bucket name) and multi-bucket catalogs.
- The two `vended-credentials` lines are harmless on a catalog that does not vend credentials; in that case your identity needs bucket access directly.
- Spark version must match the Iceberg runtime artifact: Spark 4.0 ↔ `iceberg-spark-runtime-4.0_2.13`, Spark 3.5 ↔ `iceberg-spark-runtime-3.5_2.12`.

### Path C: Serverless for Apache Spark (Dataproc Serverless) on GCP

Same Spark configuration, run as a serverless batch. Use this if you cannot run Spark from a laptop. **Network setup is the usual blocker**: in our test project the batch failed with *"Timed out waiting for at least 1 worker(s) registered ... often caused by firewall rules"* because the VPC had no intra-subnet allow rule.

Prerequisites ([Google network doc](https://docs.cloud.google.com/dataproc-serverless/docs/concepts/network)):

1. A subnet in the batch region with Private Google Access enabled.
2. A firewall rule allowing all ingress between instances in that subnet:
   ```bash
   gcloud compute firewall-rules create allow-internal-ingress \
     --network=<network> --direction=ingress --action=allow --rules=all \
     --source-ranges=<subnet-cidr> --destination-ranges=<subnet-cidr>
   ```
3. A service account for the batch with `roles/dataproc.worker` plus the roles in section 3, and read access to a staging bucket.
4. APIs enabled: `dataproc.googleapis.com`, `biglake.googleapis.com`.

Submit (runtime 2.2 ships Spark 3.5 with Iceberg built in, which is what Google's own BigLake quickstart uses):

```bash
gcloud storage cp expire_olake_snapshots.py gs://<staging-bucket>/

gcloud dataproc batches submit pyspark gs://<staging-bucket>/expire_olake_snapshots.py \
  --project=<project> --region=<region> --version=2.2 \
  --subnet=<subnet> --service-account=<sa>@<project>.iam.gserviceaccount.com \
  --properties="spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions,\
spark.sql.defaultCatalog=cat,\
spark.sql.catalog.cat=org.apache.iceberg.spark.SparkCatalog,\
spark.sql.catalog.cat.type=rest,\
spark.sql.catalog.cat.uri=https://biglake.googleapis.com/iceberg/v1/restcatalog,\
spark.sql.catalog.cat.warehouse=bl://projects/<project>/catalogs/<catalog-id>,\
spark.sql.catalog.cat.io-impl=org.apache.iceberg.gcp.gcs.GCSFileIO,\
spark.sql.catalog.cat.header.x-goog-user-project=<project>,\
spark.sql.catalog.cat.rest.auth.type=org.apache.iceberg.gcp.auth.GoogleAuthManager,\
spark.sql.catalog.cat.header.X-Iceberg-Access-Delegation=vended-credentials,\
spark.sql.catalog.cat.gcs.oauth2.refresh-credentials-endpoint=https://oauth2.googleapis.com/token" \
  -- --catalog cat --namespace <db> --dry-run
```

Then rerun without `--dry-run` and with `--retain-last 50`. We did not get to run this path end to end (network), but the Spark and catalog parts are identical to Path B.

---

## 5. After the run

1. **Confirm the newest `metadata.json` is well under 1 MB** for each table. The script prints its path; check size with:
   ```bash
   gcloud storage ls -l gs://<bucket>/<db>/<table>/<uuid>/metadata/*.metadata.json | tail -3
   ```
2. **Resume OLake jobs.** Do not reset state or re-run a full load. Confirm the next sync commits and the checkpoint advances.
3. **Do not run `remove_orphan_files` now.** `expire_snapshots` already deleted the files it could. Orphan cleanup is a separate, slower job for later, and must never run while OLake is writing.

---

## 6. Keep it from coming back

Expiry is one-shot. The table starts growing again on the next commit. If it hit 1 MB at *N* snapshots and you kept 50, you have roughly `N - 50` commits before it happens again.

1. **Schedule the same script** (daily is plenty for most CDC rates) with your orchestrator, cron, or a scheduled Dataproc batch. Run it in a window where OLake is paused, or accept that an overlapping commit may cause one retry on OLake's side.
2. **Set retention defaults on each table** so future expire runs need no arguments:
   ```sql
   ALTER TABLE <alias>.<db>.<table> SET TBLPROPERTIES (
     'history.expire.max-snapshot-age-ms'  = '86400000',   -- 1 day
     'history.expire.min-snapshots-to-keep' = '50',
     'write.metadata.delete-after-commit.enabled' = 'true',
     'write.metadata.previous-versions-max' = '20'
   );
   ```
   These properties do **not** expire anything by themselves. They are the defaults `expire_snapshots` uses when called without `older_than` / `retain_last`. The last two keep the `metadata-log` section short and delete old `metadata.json` files from the bucket; they take effect on the very next commit (in our test the `ALTER` itself trimmed `metadata-log` from 31 entries to 21). Verified against BigLake: the catalog accepts all four properties.

---

## 7. If something goes wrong

| Symptom | Meaning | Action |
|---|---|---|
| `CATALOG_NOT_FOUND ... The catalog <db> not found` warning with a stack trace during `CALL` | Spark trying to resolve the table identifier as a catalog name. Harmless, the procedure still runs. | Ignore. Check the "snapshots after" line. |
| Loading the table itself fails and the error mentions metadata size | The catalog refuses to serve a `metadata.json` already over the limit, so Spark cannot even read it. | Ask your Google account team for a temporary limit increase (the documented resolution), run the expiry, then it stays under the limit. Contact OLake support for a manual metadata-rewrite fallback if that is not possible. |
| `ClassNotFoundException: org.apache.iceberg.gcp.auth.GoogleAuthManager` | Iceberg too old, or `iceberg-gcp-bundle` missing | Use Iceberg 1.10+ and include `iceberg-gcp-bundle` in `--packages`. |
| `NoSuchMethodError` / Scala errors on startup | Spark and Iceberg runtime version mismatch | Match `iceberg-spark-runtime-<spark-version>_<scala>` to your Spark. |
| `PERMISSION_DENIED` mentioning `serviceusage.services.use` | `x-goog-user-project` header needs quota-consumer permission | Grant `roles/serviceusage.serviceUsageConsumer`, or use the OLake service account. |
| `403` on GCS object during expire | Identity lacks bucket access and the catalog does not vend credentials | Grant `roles/storage.objectUser` on the bucket to the Spark identity. |
| Serverless batch stuck in `PENDING` then fails "waiting for at least 1 worker" | VPC firewall | Path C prerequisite 2. |
| `snapshots after` equals `retain_last` but `metadata.json` still large | Many schemas / partition specs or a long `metadata-log` | Add `clean_expired_metadata => true` to the `CALL` (verified on Iceberg 1.11.0; older runtimes may reject the argument) and set the two `write.metadata.*` properties in section 6, then run once more. |
