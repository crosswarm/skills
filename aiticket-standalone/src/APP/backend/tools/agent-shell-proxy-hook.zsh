#!/usr/bin/env zsh

# Shared shell hook for Codex, Claude Code, and Gemini CLI sessions.
# It enables the localhost proxy only after direct DNS resolution fails,
# validates the proxy immediately, and restores previous proxy values on exit.

if [[ -n "${AGENT_PROXY_HOOK_LOADED:-}" ]]; then
  return 0
fi
typeset -g AGENT_PROXY_HOOK_LOADED=1

typeset -g AGENT_PROXY_HOOK_ENABLED="${AGENT_PROXY_HOOK_ENABLED:-0}"
typeset -g AGENT_PROXY_HOOK_CAPTURED="${AGENT_PROXY_HOOK_CAPTURED:-0}"
typeset -g AGENT_PROXY_HOOK_LAST_CHECK_EPOCH="${AGENT_PROXY_HOOK_LAST_CHECK_EPOCH:-0}"
typeset -g AGENT_PROXY_HOOK_INTERVAL_SECONDS="${AGENT_PROXY_HOOK_INTERVAL_SECONDS:-45}"

typeset -gr AGENT_PROXY_HOOK_HTTP_PROXY="http://127.0.0.1:6152"
typeset -gr AGENT_PROXY_HOOK_ALL_PROXY="socks5://127.0.0.1:6153"

agent_shell_proxy_hook_is_agent_shell() {
  [[ -n "${CODEX_CI:-}" ]] && return 0
  [[ -n "${CODEX_THREAD_ID:-}" ]] && return 0
  [[ -n "${CLAUDECODE:-}" ]] && return 0
  [[ -n "${CLAUDE_CODE:-}" ]] && return 0
  [[ -n "${CLAUDE_SESSION_ID:-}" ]] && return 0
  [[ -n "${GEMINI_CLI:-}" ]] && return 0
  [[ -n "${GEMINI_AGENT:-}" ]] && return 0
  [[ -n "${GOOGLE_GENAI_CLI:-}" ]] && return 0
  [[ -n "${GOOGLE_GEMINI_CLI:-}" ]] && return 0

  local cmd="${AGENT_PROXY_HOOK_PARENT_CMD:-}"
  local pid="${PPID:-}"
  local depth=0

  while [[ -z "$cmd" && -n "$pid" && "$pid" != "1" && "$depth" -lt 6 ]]; do
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    [[ "$cmd" == *codex* || "$cmd" == *claude* || "$cmd" == *gemini* ]] && return 0
    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    depth=$((depth + 1))
  done

  [[ "$cmd" == *codex* || "$cmd" == *claude* || "$cmd" == *gemini* ]]
}

agent_shell_proxy_hook_capture_originals() {
  [[ "${AGENT_PROXY_HOOK_CAPTURED}" == "1" ]] && return 0

  local lower upper key
  for key in HTTP HTTPS ALL; do
    lower="${(L)key}_proxy"
    upper="${key}_PROXY"

    if (( ${+parameters[$lower]} )); then
      typeset -g "AGENT_PROXY_HOOK_ORIG_${key}_PROXY_SET=1"
      typeset -g "AGENT_PROXY_HOOK_ORIG_${key}_PROXY=${(P)lower}"
    else
      typeset -g "AGENT_PROXY_HOOK_ORIG_${key}_PROXY_SET=0"
      typeset -g "AGENT_PROXY_HOOK_ORIG_${key}_PROXY="
    fi

    if (( ${+parameters[$upper]} )); then
      typeset -g "AGENT_PROXY_HOOK_ORIG_${key}_PROXY_UPPER_SET=1"
      typeset -g "AGENT_PROXY_HOOK_ORIG_${key}_PROXY_UPPER=${(P)upper}"
    else
      typeset -g "AGENT_PROXY_HOOK_ORIG_${key}_PROXY_UPPER_SET=0"
      typeset -g "AGENT_PROXY_HOOK_ORIG_${key}_PROXY_UPPER="
    fi
  done

  AGENT_PROXY_HOOK_CAPTURED=1
}

agent_shell_proxy_hook_apply_localhost_proxy() {
  export http_proxy="$AGENT_PROXY_HOOK_HTTP_PROXY"
  export https_proxy="$AGENT_PROXY_HOOK_HTTP_PROXY"
  export all_proxy="$AGENT_PROXY_HOOK_ALL_PROXY"
  export HTTP_PROXY="$AGENT_PROXY_HOOK_HTTP_PROXY"
  export HTTPS_PROXY="$AGENT_PROXY_HOOK_HTTP_PROXY"
  export ALL_PROXY="$AGENT_PROXY_HOOK_ALL_PROXY"
}

agent_shell_proxy_hook_restore_originals() {
  local key lower upper set_var upper_set_var value_var upper_value_var

  for key in HTTP HTTPS ALL; do
    lower="${(L)key}_proxy"
    upper="${key}_PROXY"
    set_var="AGENT_PROXY_HOOK_ORIG_${key}_PROXY_SET"
    upper_set_var="AGENT_PROXY_HOOK_ORIG_${key}_PROXY_UPPER_SET"
    value_var="AGENT_PROXY_HOOK_ORIG_${key}_PROXY"
    upper_value_var="AGENT_PROXY_HOOK_ORIG_${key}_PROXY_UPPER"

    if [[ "${(P)set_var:-0}" == "1" ]]; then
      export "$lower=${(P)value_var}"
    else
      unset "$lower"
    fi

    if [[ "${(P)upper_set_var:-0}" == "1" ]]; then
      export "$upper=${(P)upper_value_var}"
    else
      unset "$upper"
    fi
  done
}

agent_shell_proxy_hook_direct_dns_ok() {
  if [[ "${AGENT_PROXY_HOOK_TEST_MODE:-0}" == "1" ]]; then
    [[ "${AGENT_PROXY_HOOK_TEST_DIRECT_RESULT:-ok}" == "ok" ]]
    return $?
  fi

  env -u http_proxy -u https_proxy -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    python3 - <<'PY' >/dev/null 2>&1
import socket
for host in ("example.com", "www.baidu.com"):
    socket.gethostbyname(host)
PY
}

agent_shell_proxy_hook_proxy_ok() {
  if [[ "${AGENT_PROXY_HOOK_TEST_MODE:-0}" == "1" ]]; then
    [[ "${AGENT_PROXY_HOOK_TEST_PROXY_RESULT:-ok}" == "ok" ]]
    return $?
  fi

  curl -I --silent --output /dev/null --max-time 5 https://example.com
}

agent_shell_proxy_hook_maybe_enable() {
  agent_shell_proxy_hook_is_agent_shell || return 0
  [[ "${AGENT_PROXY_HOOK_ENABLED}" == "1" ]] && return 0

  if agent_shell_proxy_hook_direct_dns_ok; then
    return 0
  fi

  agent_shell_proxy_hook_capture_originals
  agent_shell_proxy_hook_apply_localhost_proxy

  if agent_shell_proxy_hook_proxy_ok; then
    AGENT_PROXY_HOOK_ENABLED=1
    print -u2 -- "[agent-proxy-hook] DNS failed. Enabled localhost proxy 127.0.0.1:6152/6153."
    return 0
  fi

  print -u2 -- "[agent-proxy-hook] DNS failed, and localhost proxy validation also failed. Restoring previous proxy settings."
  agent_shell_proxy_hook_restore_originals
  AGENT_PROXY_HOOK_ENABLED=0
  return 1
}

agent_shell_proxy_hook_precmd() {
  agent_shell_proxy_hook_is_agent_shell || return 0

  local now="${EPOCHSECONDS:-0}"
  local elapsed=$(( now - AGENT_PROXY_HOOK_LAST_CHECK_EPOCH ))

  if [[ "${AGENT_PROXY_HOOK_LAST_CHECK_EPOCH}" != "0" && "$elapsed" -lt "${AGENT_PROXY_HOOK_INTERVAL_SECONDS}" ]]; then
    return 0
  fi

  AGENT_PROXY_HOOK_LAST_CHECK_EPOCH="$now"
  agent_shell_proxy_hook_maybe_enable || true
}

agent_shell_proxy_hook_cleanup() {
  if [[ "${AGENT_PROXY_HOOK_CAPTURED}" == "1" ]]; then
    agent_shell_proxy_hook_restore_originals
  fi
  AGENT_PROXY_HOOK_ENABLED=0
}

if [[ -o interactive ]]; then
  autoload -Uz add-zsh-hook
  add-zsh-hook precmd agent_shell_proxy_hook_precmd
  add-zsh-hook zshexit agent_shell_proxy_hook_cleanup
fi
