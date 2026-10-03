"""The ClickHouse store (specs/ch-store.md): every scan of a deployment as
interval-versioned rows, one table, ingested daily (`ingest`) and answered in
the Worker's response shapes (`serve`, behind `dt-cloud serve-query -e ch`)."""
