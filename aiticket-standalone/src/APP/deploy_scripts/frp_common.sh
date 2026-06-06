#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FRP_VERSION="${FRP_VERSION:-0.52.3}"
FRP_DIR="${FRP_DIR:-$SCRIPT_DIR/frp}"

source "$SCRIPT_DIR/network_env.sh"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

require_env() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        error "缺少环境变量: $name"
        exit 1
    fi
}

_detect_frp_platform() {
    local os arch
    case "$(uname -s)" in
        Linux) os="linux" ;;
        Darwin) os="darwin" ;;
        *)
            error "不支持的操作系统: $(uname -s)"
            exit 1
            ;;
    esac

    case "$(uname -m)" in
        x86_64|amd64) arch="amd64" ;;
        arm64|aarch64) arch="arm64" ;;
        *)
            error "不支持的架构: $(uname -m)"
            exit 1
            ;;
    esac

    echo "${os}:${arch}"
}

ensure_frp_binary() {
    local binary_name="$1"
    if [ -x "$FRP_DIR/$binary_name" ]; then
        if "$FRP_DIR/$binary_name" --version >/dev/null 2>&1; then
            return 0
        fi
        warn "检测到不可用的 ${binary_name} 二进制，重新安装"
    fi

    mkdir -p "$FRP_DIR"

    local platform os arch archive_name archive_path archive_url extracted_dir
    platform="$(_detect_frp_platform)"
    os="${platform%%:*}"
    arch="${platform##*:}"
    archive_name="frp_${FRP_VERSION}_${os}_${arch}.tar.gz"
    archive_path="$SCRIPT_DIR/$archive_name"
    archive_url="https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${archive_name}"
    extracted_dir="$SCRIPT_DIR/frp_${FRP_VERSION}_${os}_${arch}"

    if [ ! -f "$archive_path" ]; then
        info "下载 frp ${FRP_VERSION} (${os}/${arch})"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL -o "$archive_path" "$archive_url"
        else
            wget -O "$archive_path" "$archive_url"
        fi
    fi

    rm -rf "$extracted_dir" "$FRP_DIR"
    tar -xzf "$archive_path" -C "$SCRIPT_DIR"
    mv "$extracted_dir" "$FRP_DIR"
}

render_frpc_config() {
    local target="$FRP_DIR/frpc.ini"
    cp "$PROJECT_ROOT/APP/backend/config/frpc.ini" "$target"

    require_env "FRP_TOKEN"

    export FRP_SERVER_ADDR="${FRP_SERVER_ADDR:-}"
    export FRP_CUSTOM_DOMAIN="${FRP_CUSTOM_DOMAIN:-localhost}"
    export FRP_CONNECT_SERVER_LOCAL_IP="${FRP_CONNECT_SERVER_LOCAL_IP:-}"

    perl -0pi -e 's/__FRP_TOKEN__/$ENV{"FRP_TOKEN"}/g' "$target"
    perl -0pi -e 's/^server_port = .*/server_port = $ENV{"FRP_BIND_PORT"}/m' "$target"
    perl -0pi -e 's/^local_port = .*/local_port = $ENV{"MINI_PROXY_PORT"}/m' "$target"

    if [ -n "$FRP_SERVER_ADDR" ]; then
        perl -0pi -e 's/^server_addr = .*/server_addr = $ENV{"FRP_SERVER_ADDR"}/m' "$target"
    fi

    if [ -n "$FRP_CUSTOM_DOMAIN" ]; then
        perl -0pi -e 's/^custom_domains = .*/custom_domains = $ENV{"FRP_CUSTOM_DOMAIN"}/m' "$target"
    fi

    if [ -n "$FRP_CONNECT_SERVER_LOCAL_IP" ]; then
        if grep -q '^connect_server_local_ip = ' "$target"; then
            perl -0pi -e 's/^connect_server_local_ip = .*/connect_server_local_ip = $ENV{"FRP_CONNECT_SERVER_LOCAL_IP"}/m' "$target"
        else
            perl -0pi -e 's/^server_port = .*\n/$&connect_server_local_ip = $ENV{"FRP_CONNECT_SERVER_LOCAL_IP"}\n/m' "$target"
        fi
    fi
}

render_frps_config() {
    local target="$FRP_DIR/frps.ini"
    cp "$PROJECT_ROOT/APP/backend/config/frps.ini" "$target"

    require_env "FRP_TOKEN"
    require_env "FRP_DASHBOARD_PWD"

    perl -0pi -e 's/__FRP_TOKEN__/$ENV{"FRP_TOKEN"}/g' "$target"
    perl -0pi -e 's/__FRP_DASHBOARD_PWD__/$ENV{"FRP_DASHBOARD_PWD"}/g' "$target"
    perl -0pi -e 's/^bind_port = .*/bind_port = $ENV{"FRP_BIND_PORT"}/m' "$target"
    perl -0pi -e 's/^allow_ports = .*/allow_ports = $ENV{"MINI_PROXY_PORT"}/m' "$target"
    perl -0pi -e 's/^vhost_http_port = .*/vhost_http_port = $ENV{"FRP_VHOST_HTTP_PORT"}/m' "$target"
    perl -0pi -e 's/^dashboard_port = .*/dashboard_port = $ENV{"FRP_DASHBOARD_PORT"}/m' "$target"
}
