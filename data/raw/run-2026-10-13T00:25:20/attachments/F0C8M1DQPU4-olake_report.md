# OLake job 146 OOM — diagnostic report

Postgres → Iceberg full refresh, OLake v0.12.0, GKE.
Crash captured: **2026-10-11 01:01:33 UTC**, pod `sync-123-146-2026-10-11t01-00-00z`.

All evidence below is from the real failure (kernel OOM dump + cAdvisor history), not a reproduction.

---

## 1. Verdict

The sync pod grew to **~22.6 GiB RSS in ~90 seconds** and was killed by the kernel when the
node's `kubepods.slice` cgroup hit its ceiling. Both processes in the pod were killed:

| process | anon-rss | total-vm |
|---|---|---|
| `java` (Iceberg writer, pid 690688) | **11.45 GiB** (12,007,688 kB) | 29.3 GiB |
| `olake` (Go connector, pid 690561/690600) | **11.13 GiB** (11,671,996 kB) | 12.6 GiB |

At that moment the connector had pushed **one** batch of 8,361 records and had a second in
flight — i.e. roughly **2 GB of source bytes in flight produced ~22.6 GiB of RSS (~11x)**.

---

## 2. Kernel OOM dump

Source: GCP serial console log, node `gke-star-rocks-clust-olake-job-nodepo-8ac0733c-z85w`
(this is the `dmesg` output that was requested — the node has since been deleted, see §6).

```
[126004.832375] olake invoked oom-killer: gfp_mask=0x100cca(GFP_HIGHUSER_MOVABLE), order=0, oom_score_adj=993
[126004.842243] CPU: 7 UID: 0 PID: 690600 Comm: olake Not tainted 6.12.94+ #1
[126004.979240] memory: usage 29037432kB, limit 29029444kB, failcnt 403935
[126004.993032] anon 29633077248
[126005.181582] Tasks state (memory values in pages):
[126005.667158] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),
                oom_memcg=/kubepods.slice,
                task_memcg=/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod501d708c_cc30_41e0_94e0_092157c5c263.slice/...,
                task=java,pid=690688,uid=0
[126005.705243] Memory cgroup out of memory: Killed process 690688 (java)  total-vm:30703192kB, anon-rss:12007688kB, file-rss:224kB, pgtables:24472kB, oom_score_adj:993
[126005.745502] Memory cgroup out of memory: Killed process 690561 (olake) total-vm:13169024kB, anon-rss:11671996kB, file-rss:248kB, pgtables:23264kB, oom_score_adj:993
[126005.772362] Memory cgroup out of memory: Killed process 690600 (olake) total-vm:13169024kB, anon-rss:11671996kB, file-rss:248kB, pgtables:23264kB, oom_score_adj:993
```

Key points:

* `oom_memcg=/kubepods.slice` with `limit 29029444kB` (27.7 GiB) — the kill happened at the
  **node's pod-allocatable cgroup**, not at a container limit. The sync pod has no memory
  limit (`requests: 256Mi/100m`, no limits), so nothing stopped it before the node ran out.
* The Go process (`olake`) is the one that *triggered* the OOM, but **both** the Go connector
  and the Java writer were sitting on ~11.5 GiB each.
* `oom_score_adj: 993` — a consequence of the 256Mi request vs. 22 GiB actual usage.

---

## 3. Memory over time (cAdvisor, `container_memory_working_set_bytes`)

Pod `sync-123-146-2026-10-11t01-00-00z`, container `connector`:

Query (cAdvisor scrape interval ~20 s; the image filter excludes the unrelated Kafka syncs):

```promql
container_memory_working_set_bytes{namespace="olake", pod=~"sync.*", image!~".*kafka.*", container!=""}
```

| time (UTC) | working set | RSS | delta (working set) |
|---|---|---|---|
| 01:00:10 | 0.00 GiB | 0.00 GiB | |
| 01:00:20 | 0.81 GiB | 0.60 GiB | +0.80 |
| 01:00:40 | 2.38 GiB | 2.17 GiB | +1.57 |
| 01:00:50 | 5.68 GiB | 5.46 GiB | +3.30 |
| 01:01:00 | 13.97 GiB | 13.74 GiB | +8.29 |
| 01:01:20 | 17.99 GiB | 17.74 GiB | +4.02 |
| 01:01:40 | **22.13 GiB** | **21.87 GiB** | +4.13 |
| 01:01:33 | **killed** — kernel dump totals 22.58 GiB anon-rss (11.45 java + 11.13 olake) | | |

**0 → 22.13 GiB in ~90 seconds** (~250 MB/s of growth) from the moment the backfill threads
started reading. The cAdvisor peak (22.13 GiB working set / 21.87 GiB RSS) corroborates the
kernel's 22.58 GiB figure independently.

Scanning the whole of 2026-10-11 with the filter above returns **exactly one** series — this
pod — confirming job 146 ran once that day and that no other Postgres sync contributed.

---

## 4. Correlated connector timeline (`olake.log`)

```
01:00:08  Dropping Iceberg table: lenskart_migration_db.lenskart_last_year_cases   # full refresh clears destination
01:00:11  Started shared Iceberg JVM on port 50051
01:00:12  Starting backfill for stream[lenskart_last_year_cases] with 9 chunks
01:00:12  created writer for chunk min['(1048576,0)'] max['(1310720,0)']
01:00:12  created writer for chunk min['(0,0)']       max['(262144,0)']      # max_threads=2
01:01:31  Successfully wrote 8361 events for thread: ...  across 0 files      # 27,086 ms gRPC call
01:01:33  <OOM kill>
01:01:36  worker.log: pod failed (exit code: 137, reason: OOMKilled)
state.json: {}                                                                # nothing checkpointed
```

`across 0 files` = the writer was still accumulating in memory when the pod died.

---

## 5. Why the numbers come out this way (v0.12.0 code references)

1. **Chunking ignores TOAST.** `drivers/postgres/internal/backfill.go` sizes ctid chunks as
   `constants.EffectiveParquetSize` (2 GB) ÷ block size → 262,144 blocks per chunk, derived
   from `relpages` — heap only. This table is 16 GB of heap but **4,488 GB of TOAST**, so
   "9 chunks" is really ~8.7M rows / ~500 GB of JSON per chunk.
2. **Flush is triggered by the 1 GB byte cap.** `destination/writers.go:253` flushes at
   10,000 records **or** `constants.MaxDestinationBatchBytes` (1 GB). At ~131 KB/row the byte
   cap fires first: 8,361 × 131 KB ≈ 1.07 GB — matching the log exactly.
3. **The legacy writer materialises that batch in the JVM.** `destination/iceberg/iceberg.go:146`
   selects `legacywriter` unless `arrow_writes` is set; the whole batch crosses gRPC and is
   expanded in heap (`Server started on port 50051 with max message size: 2047MB`).
4. **The JVM's heap ceiling comes from the node, not the pod.**
   `destination/iceberg/java_client.go:188` launches
   `java -XX:+UseG1GC -XX:MaxRAMPercentage=75.0 -XX:+ExitOnOutOfMemoryError -jar …`.
   With no container memory limit, 75% is computed against the **node's** 32 GB → ~24 GB
   ceiling, so G1 expands instead of collecting.
5. **No per-job resources available.** `olake-helm/worker/executor/kubernetes/pod.go:320`
   hardcodes `requests: 256Mi/100m` and sets no limits; configurable resources are an open,
   unmerged PR (olake-helm#218).
6. **Full refresh cannot resume.** `protocol/sync.go:122` runs `ClearState` + `DropStreams`
   for full-refresh streams on *every* run, which is why `state.json` is `{}` and why each
   attempt re-drops the destination and restarts from zero.

Observed amplification: ~2 GB of source bytes in flight → ~22.6 GiB RSS (Go ~11.1 + JVM ~11.5).

---

## 6. Node and co-tenancy

At the time of the crash, pod memory on node `…olake-job-nodepo-8ac0733c-z85w`
(8 vCPU / 32 GB, 28.9 GiB allocatable, `kubepods.slice` limit 27.7 GiB):

| namespace | pod | working set @01:01:30 |
|---|---|---|
| olake | **sync-123-146-…** (this job) | **17.99 GiB** (peak 22.13 GiB at 01:01:40) |
| olake | sync-123-141-… (unrelated Kafka sync) | 3.45 GiB |
| lakekeeper | lakekeeper-69768bc894-zxdjj | 1.08 GiB |
| kube-system | 6 daemonset pods | ~0.21 GiB total |

So ~4.7 GiB was already in use by other pods; the sync pod consumed the rest.

**Environment has changed since the crash (2026-10-12):** the dedicated
`olake-job-nodepool` (8 vCPU / 32 GB) **no longer exists**. The only OLake nodes left are
`olake-worker-nodepool` (`e2-custom-6-18432` — 6 vCPU / 18 GB, ~15.4 GiB allocatable,
autoscale 1–2), and they currently host olake-ui, olake-workers, temporal, olake-fusion and
the Amoro optimizer (node `…be9n` is at 80% memory).

Implication: re-running the job unchanged today would hit the `kubepods.slice` ceiling even
sooner (~15.4 GiB instead of 27.7 GiB) and the kernel's victim set would include
**olake-workers / temporal / olake-ui** — i.e. the orchestrator could kill itself. A rerun
should either wait for a dedicated large node pool, or go out with the JVM heap capped.

---

## 7. Source table profile (the reason this is unusual)

Source: `migration` / `public.lenskart_last_year_cases` (Postgres 18.4)
Target: `lenskart_migration_db.lenskart_last_year_cases` (Iceberg, Lakekeeper REST, Wasabi)

```
rows            : 78,523,153
heap            : 16 GB
TOAST           : 4,488 GB      <- two large jsonb columns
indexes         : 23 GB
total           : 4,527 GB
avg JSON/row    : ~131 KB  (ticket_response ~39.5 KB + conversation_response ~79.7 KB)
total on wire   : ~10.3 TB
dead tuples     : 10.8M, last autovacuum 2026-07-01
```

Measured read throughput from a pod in the same VPC: **~27 MB/s per connection**
(1,000 rows = ~131 MB in 4.77 s). At `max_threads: 2` that is ~50+ hours of reading alone.

---

## 8. Current configuration

Source (`postgres`, v0.12.0) — credentials redacted:

```json
{
  "host": "<redacted>", "port": 5432, "database": "migration",
  "username": "<redacted>", "password": "<redacted>",
  "ssl": {"mode": "disable"}, "ssh_config": {"type": "no_tunnel"},
  "schemas": [], "max_threads": 2, "retry_count": 5,
  "update_method": {"type": "Standalone"}
}
```

| item | value |
|---|---|
| job | id 146, `kapture_lenskart_migration_job_3`, active |
| stream | `public.lenskart_last_year_cases`, sync mode **full_refresh**, no `chunk_column`, no `append_mode` |
| destination | Iceberg, REST catalog (Lakekeeper 0.10.4), Wasabi S3 (ap-southeast-1), remote signing |
| writer | **legacy** (`arrow_writes` not set) — log: `Legacy writer enabled - registered OlakeRowsIngester service` |
| sync pod | `olakego/source-postgres:v0.12.0`, requests 256Mi/100m, **no limits** |

---

## 9. Changes we plan to make (pending OLake guidance)

1. `JAVA_TOOL_OPTIONS=-Xmx8g` via the (already wired, currently empty) `olake-global-env`
   ConfigMap, so the writer JVM stops sizing its heap from node RAM.
2. `"arrow_writes": true` on the Iceberg destination, to keep large buffers out of the JVM.
3. `chunk_column: id` on the stream (int PK) → ~8,600 chunks of 10k ids instead of 9 ctid
   chunks of ~8.7M rows.
4. Switch the stream to **incremental** (cursor `id`) + `append_mode`, so the backfill
   checkpoints and a failure resumes instead of re-dropping the table.
5. Restore a dedicated job node pool before the real run.

## 10. If a reproduction run is still wanted

The pod lives ~90 s, so sampling must be armed before the job is triggered. Note the
connector image (`eclipse-temurin:17-jre-noble`) ships **without `procps`**, so
`ps -eo pid,rss,comm` does not work inside it; read `/proc/<pid>/status` instead:

```bash
kubectl -n olake exec <sync-pod> -- sh -c \
  'for p in /proc/[0-9]*; do \
     n=$(tr -d "\0" < $p/comm 2>/dev/null); \
     r=$(awk "/VmRSS/{print \$2}" $p/status 2>/dev/null); \
     [ -n "$r" ] && echo "$p $n ${r}kB"; \
   done | sort -k3 -t" " -rn | head -5'
```

Plus `container_memory_working_set_bytes{pod=~"sync-123-146.*"}` in Prometheus (already
available at `:9090`) and the serial-console query used for §2:

```bash
gcloud logging read \
  'logName="projects/<project>/logs/serialconsole.googleapis.com%2Fserial_port_1_output"
   AND timestamp>="<T0>" AND timestamp<="<T1>"
   AND (textPayload:"Killed process" OR textPayload:"oom-kill:")' \
  --project <project> --order asc --format="value(timestamp,textPayload)"
```
