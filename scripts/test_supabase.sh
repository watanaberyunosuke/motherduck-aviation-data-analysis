#!/usr/bin/env bash
# Runs the Supabase migrations and supabase/tests on a throwaway local Postgres.
# Needs Postgres 15 or later (initdb, pg_ctl, psql on PATH, or PG_BIN set).
set -euo pipefail
cd "$(dirname "$0")/.."
PG_BIN="${PG_BIN:-$(dirname "$(command -v initdb || ls -d /usr/lib/postgresql/*/bin/initdb | tail -1)")}"
dir="$(mktemp -d)"
trap '"$PG_BIN/pg_ctl" -D "$dir/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$dir"' EXIT
"$PG_BIN/initdb" -D "$dir/data" -U postgres -A trust >/dev/null
"$PG_BIN/pg_ctl" -D "$dir/data" -o "-k $dir -c listen_addresses=''" -l "$dir/log" start >/dev/null
psql=(psql -h "$dir" -U postgres -d postgres -v ON_ERROR_STOP=1 -q)
"${psql[@]}" -f supabase/tests/auth_stub.sql
for f in supabase/migrations/*.sql; do "${psql[@]}" -f "$f"; done
"${psql[@]}" -f supabase/tests/accounts_test.sql
echo "Supabase tests passed"
