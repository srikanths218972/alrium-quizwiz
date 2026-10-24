ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${QA_ENV_FILE:-$ROOT/.env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    . "$ENV_FILE"
    set +a
fi
if [[ -z "${DATABASE_URL:-}" && -n "${POSTGRES_USER:-}" ]]; then
    export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${QA_DB_PORT:-5433}/${POSTGRES_DB}"
fi
describe_url() { python3 -c 'import sys,urllib.parse as u; p=u.urlparse(sys.argv[1]); print(f"{p.username}@{p.hostname}:{p.port or 5432}{p.path}")' "$1"; }
compose() { docker compose --project-directory "$ROOT" -f "$ROOT/compose.yaml" --env-file "$ENV_FILE" "$@"; }
backend() { (cd "$ROOT/backend" && uv run --frozen "$@"); }
port_free() {
    local pids; pids="$(lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')"
    [[ -z "$pids" ]] && return 0
    echo "error: port $1 is already in use by PID(s) $pids($(ps -o command= -p "${pids%% *}" | cut -c1-80))." >&2
    echo "       Stop that process (kill $pids) or set $2 in .env to another port." >&2
    return 1
}
