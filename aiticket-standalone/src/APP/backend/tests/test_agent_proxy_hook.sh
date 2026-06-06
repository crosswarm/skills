#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOOK_PATH="$ROOT_DIR/APP/backend/tools/agent-shell-proxy-hook.zsh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

run_zsh_test() {
  local test_name="$1"
  local script_body="$2"

  zsh <<ZSH || fail "$test_name"
set -euo pipefail
export AGENT_PROXY_HOOK_TEST_MODE=1
source "$HOOK_PATH"
$script_body
ZSH
}

run_zsh_test "detects agent shells by env prefix" '
unset CODEX_CI CODEX_THREAD_ID CLAUDECODE CLAUDE_CODE GEMINI_CLI GEMINI_AGENT GOOGLE_GENAI_CLI || true
AGENT_PROXY_HOOK_PARENT_CMD="iTerm"
agent_shell_proxy_hook_is_agent_shell && exit 1
export CLAUDE_CODE=1
agent_shell_proxy_hook_is_agent_shell
'

run_zsh_test "enables localhost proxy and preserves originals" '
export http_proxy=http://old-http:9999
unset https_proxy all_proxy || true
export AGENT_PROXY_HOOK_TEST_DIRECT_RESULT=fail
export AGENT_PROXY_HOOK_TEST_PROXY_RESULT=ok
agent_shell_proxy_hook_maybe_enable
[[ "${http_proxy}" == "http://127.0.0.1:6152" ]]
[[ "${https_proxy}" == "http://127.0.0.1:6152" ]]
[[ "${all_proxy}" == "socks5://127.0.0.1:6153" ]]
[[ "${AGENT_PROXY_HOOK_ENABLED}" == "1" ]]
[[ "${AGENT_PROXY_HOOK_ORIG_HTTP_PROXY_SET}" == "1" ]]
[[ "${AGENT_PROXY_HOOK_ORIG_HTTP_PROXY}" == "http://old-http:9999" ]]
[[ "${AGENT_PROXY_HOOK_ORIG_HTTPS_PROXY_SET}" == "0" ]]
'

run_zsh_test "restores prior values on cleanup" '
export http_proxy=http://before-http:1234
export https_proxy=http://before-https:5678
unset all_proxy || true
export AGENT_PROXY_HOOK_TEST_DIRECT_RESULT=fail
export AGENT_PROXY_HOOK_TEST_PROXY_RESULT=ok
agent_shell_proxy_hook_maybe_enable
agent_shell_proxy_hook_cleanup
[[ "${http_proxy}" == "http://before-http:1234" ]]
[[ "${https_proxy}" == "http://before-https:5678" ]]
[[ -z "${all_proxy:-}" ]]
[[ "${AGENT_PROXY_HOOK_ENABLED}" == "0" ]]
'

run_zsh_test "rolls back when proxy validation fails" '
export http_proxy=http://old-http:9999
unset https_proxy all_proxy || true
export AGENT_PROXY_HOOK_TEST_DIRECT_RESULT=fail
export AGENT_PROXY_HOOK_TEST_PROXY_RESULT=fail
! agent_shell_proxy_hook_maybe_enable
[[ "${http_proxy}" == "http://old-http:9999" ]]
[[ -z "${https_proxy:-}" ]]
[[ -z "${all_proxy:-}" ]]
[[ "${AGENT_PROXY_HOOK_ENABLED}" == "0" ]]
'

echo "PASS: agent shell proxy hook"
