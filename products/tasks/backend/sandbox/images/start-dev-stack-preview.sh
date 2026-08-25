#!/usr/bin/env bash
set -Eeuo pipefail

STATE_DIR=/tmp/posthog-preview
STATUS_FILE="$STATE_DIR/status.json"
LOCK_FILE="$STATE_DIR/lock"
CADDYFILE="$STATE_DIR/Caddyfile"
PROXY_CONTAINER=posthog-preview-proxy
WEB_HEALTH_URL=http://localhost:8010/_health
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-360}"

mkdir -p "$STATE_DIR"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

write_status() {
	state="$1"
	error="$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n\r\t' '   ' | cut -c1-500)"
	{
		printf '{"state":"%s","started_at":"%s","updated_at":"%s"' "$state" "$STARTED_AT" "$(now)"
		if [ -n "$error" ]; then
			printf ',"error":"%s"' "$error"
		fi
		printf '}\n'
	} >"$STATUS_FILE.tmp" && mv "$STATUS_FILE.tmp" "$STATUS_FILE"
}

fail() {
	trap - ERR
	echo "preview_failed: $1" >&2
	write_status failed "$1"
	exit 1
}

on_unexpected_exit() {
	trap - ERR
	echo "preview_failed: unexpected exit at line $1" >&2
	write_status failed "unexpected exit at line $1"
	exit 1
}

proxy_is_running() {
	[ "$(docker inspect -f '{{.State.Running}}' "$PROXY_CONTAINER" 2>/dev/null || true)" = "true" ]
}

web_is_healthy() {
	curl -sf --max-time 5 "$WEB_HEALTH_URL" >/dev/null 2>&1
}

STARTED_AT="$(now)"
trap 'on_unexpected_exit $LINENO' ERR

# One launcher at a time. A second invocation (workflow retry, continue-as-new,
# sandbox rotation re-entry) leaves the running one alone and keeps its status.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
	echo "preview_already_launching"
	exit 0
fi

# Re-entry against a stack that is already serving: publish ready and touch nothing.
if web_is_healthy && proxy_is_running; then
	write_status ready
	echo "preview_already_ready"
	exit 0
fi

write_status starting

[ -n "${MODAL_HOST:-}" ] || fail "MODAL_HOST is required"
[ -n "${PREVIEW_PORT:-}" ] || fail "PREVIEW_PORT is required"
[ -n "${REPO_PATH:-}" ] || fail "REPO_PATH is required"

cd "$REPO_PATH" || fail "repository checkout $REPO_PATH is missing"

cat >"$CADDYFILE" <<CADDY
{
	auto_https off
	admin off
}
:${PREVIEW_PORT} {
	@metrics path /_metrics /_metrics/*
	handle @metrics {
		respond 404
	}
	@vite_ws {
		header Connection *Upgrade*
		header Upgrade websocket
		query token=*
	}
	@vite {
		path /@*
		path /src/*
		path /node_modules/*
		path /__vite_ping
	}
	handle @vite_ws {
		reverse_proxy 127.0.0.1:8234
	}
	handle @vite {
		reverse_proxy 127.0.0.1:8234 {
			header_up X-Forwarded-Proto https
		}
	}
	handle {
		reverse_proxy 127.0.0.1:8010 {
			header_up X-Forwarded-Proto https
		}
	}
}
CADDY

echo "== bootstrap-dev-stack =="
if [ -x /usr/local/bin/bootstrap-dev-stack ]; then
	/usr/local/bin/bootstrap-dev-stack || fail "bootstrap-dev-stack failed"
fi

echo "== preview proxy =="
if proxy_is_running; then
	echo "preview proxy already running"
else
	docker rm -f "$PROXY_CONTAINER" >/dev/null 2>&1 || true
	docker run -d --name "$PROXY_CONTAINER" --network host \
		-v "$CADDYFILE":/etc/caddy/Caddyfile:ro \
		caddy:latest caddy run -c /etc/caddy/Caddyfile ||
		fail "could not start the preview proxy"
fi

echo "== pnpm install =="
pnpm install --frozen-lockfile --prefer-offline || fail "pnpm install failed"

echo "== uv sync =="
uv sync || fail "uv sync failed"

# The venv activate script reads variables an unset-strict shell would trip over.
activate_failed=
set +u
# shellcheck disable=SC1091
source .venv/bin/activate || activate_failed=1
set -u
[ -z "$activate_failed" ] || fail "could not activate the python environment"

export MODAL_HOST
export CADDY_HOST=:8000
export SITE_URL="https://$MODAL_HOST"
export JS_URL="https://$MODAL_HOST:443"
export VITE_ALLOWED_HOSTS="$MODAL_HOST"
export IS_BEHIND_PROXY=1
export TRUST_ALL_PROXIES=1
export DEBUG=1
export COMPOSE_PROJECT_NAME=posthog
export HOGLI_SKIP_ZOMBIE_CHECK=1

echo "== hogli start =="
hogli start -y -d || fail "hogli start failed"

echo "== waiting for the web server =="
web_ready=0
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
while [ "$SECONDS" -lt "$deadline" ]; do
	if web_is_healthy; then
		web_ready=1
		break
	fi
	sleep 3
done
[ "$web_ready" -eq 1 ] || fail "the web server did not answer /_health within ${HEALTH_TIMEOUT_SECONDS}s"

echo "== setup_dev =="
python manage.py setup_dev --no-data || echo "setup_dev failed; the preview has no seeded login"

write_status ready
echo "preview_ready"
