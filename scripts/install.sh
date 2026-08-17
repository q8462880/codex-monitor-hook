#!/bin/sh
# macOS/Linux 安装入口：复用用户现有的 python3，不创建系统服务。

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
CODEX_HOME=${CODEX_HOME:-"$HOME/.codex"}
TARGET_DIR=${CODEX_SCREEN_TARGET_DIR:-"$HOME/.codex_screen"}
HOOK_PROFILE=${CODEX_SCREEN_HOOK_PROFILE:-quota}
SKIP_CONFIG_UPDATE=0
SKIP_HOOK_DIAGNOSTIC=0
INSTALL_STARTED_AT=$(date +%s)

usage() {
    printf '%s\n' "Usage: $0 [--hook-profile quota|full|minimal] [--codex-home PATH] [--target-dir PATH] [--skip-config-update] [--skip-hook-diagnostic]"
}

expected_hook_count() {
    case "$1" in
        quota) printf '%s\n' 3 ;;
        minimal) printf '%s\n' 5 ;;
        full) printf '%s\n' 11 ;;
    esac
}

verify_hook_block() {
    expected_count=$1
    actual_count=$(awk '
        /^# BEGIN codex-monitor-hook$/ { inside_block=1; next }
        /^# END codex-monitor-hook$/ { inside_block=0 }
        inside_block && /^\[\[hooks\./ { hook_count++ }
        END { print hook_count + 0 }
    ' "$CODEX_HOME/config.toml")
    if [ "$actual_count" -ne "$expected_count" ]; then
        printf '%s\n' "Hook config verification failed: expected $expected_count hooks, found $actual_count." >&2
        exit 1
    fi
}

log_elapsed() {
    current_time=$(date +%s)
    printf '%s\n' "[install] $1: $((current_time - INSTALL_STARTED_AT))s"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --hook-profile)
            HOOK_PROFILE=${2:?missing hook profile}
            shift 2
            ;;
        --codex-home)
            CODEX_HOME=${2:?missing Codex home}
            shift 2
            ;;
        --target-dir)
            TARGET_DIR=${2:?missing runtime directory}
            shift 2
            ;;
        --skip-config-update)
            SKIP_CONFIG_UPDATE=1
            shift
            ;;
        --skip-hook-diagnostic)
            SKIP_HOOK_DIAGNOSTIC=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf '%s\n' "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

case "$HOOK_PROFILE" in
    quota|full|minimal) ;;
    *) printf '%s\n' "Invalid hook profile: $HOOK_PROFILE" >&2; exit 2 ;;
esac

# 子进程必须继承同一个配置目录，否则自定义 --codex-home 的安装会写入正确文件，
# 但 app-server 却从默认目录读取，导致 Hook 诊断和信任状态错位。
export CODEX_HOME
EXPECTED_HOOK_COUNT=$(expected_hook_count "$HOOK_PROFILE")

PYTHON=${CODEX_SCREEN_PYTHON:-}
if [ -z "$PYTHON" ]; then
    PYTHON=$(command -v python3 2>/dev/null || true)
fi
if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
    printf '%s\n' "A working python3 is required. Install Python 3 and rerun this installer." >&2
    exit 1
fi
if ! "$PYTHON" -c 'import tomllib' >/dev/null 2>&1; then
    # Python 3.9/3.10 没有标准库 tomllib。tomli 只供安装阶段校验 config.toml，
    # 不会被 daemon 或 Hook relay 导入，避免要求用户升级现有系统 Python。
    if ! "$PYTHON" -c 'import tomli' >/dev/null 2>&1; then
        printf '%s\n' "Installing the Python tomli package for config validation..."
        "$PYTHON" -m pip install --user tomli --disable-pip-version-check --timeout 15 --retries 1
    fi
    "$PYTHON" -c 'import tomli' >/dev/null 2>&1 || {
        printf '%s\n' "tomli is unavailable for the selected Python." >&2
        exit 1
    }
fi

if ! "$PYTHON" -c 'import hid' >/dev/null 2>&1; then
    printf '%s\n' "Installing the Python hidapi package..."
    "$PYTHON" -m pip install --user hidapi --disable-pip-version-check --timeout 15 --retries 1
fi
"$PYTHON" -c 'import hid' >/dev/null 2>&1 || {
    printf '%s\n' "hidapi is unavailable in the selected Python." >&2
    exit 1
}
log_elapsed "Python dependency check"

mkdir -p "$CODEX_HOME" "$TARGET_DIR" "$HOME/.codex/skills/codex-monitor-hook"
STAMP=$(date +%Y%m%d-%H%M%S)
PACKAGE_TARGET="$HOME/.codex/codex-monitor-hook"
if [ "$REPO_ROOT" != "$PACKAGE_TARGET" ] && [ -e "$PACKAGE_TARGET" ]; then
    mv "$PACKAGE_TARGET" "$PACKAGE_TARGET.bak.$STAMP"
fi
if [ "$REPO_ROOT" != "$PACKAGE_TARGET" ]; then
    mkdir -p "$PACKAGE_TARGET"
    find "$REPO_ROOT" -maxdepth 1 -type f -exec cp -p {} "$PACKAGE_TARGET/" \;
    mkdir -p "$PACKAGE_TARGET/scripts" "$PACKAGE_TARGET/tests" "$PACKAGE_TARGET/references" "$PACKAGE_TARGET/.codex"
    find "$REPO_ROOT/scripts" -maxdepth 1 -type f -name '*.py' -exec cp -p {} "$PACKAGE_TARGET/scripts/" \;
    cp -p "$REPO_ROOT/scripts/install.ps1" "$PACKAGE_TARGET/scripts/"
    cp -p "$REPO_ROOT/scripts/install.sh" "$PACKAGE_TARGET/scripts/"
    cp -p "$REPO_ROOT/.codex/INSTALL.md" "$PACKAGE_TARGET/.codex/"
    cp -p "$REPO_ROOT/references/codex_config_hooks.toml" "$PACKAGE_TARGET/references/"
    find "$REPO_ROOT/tests" -maxdepth 1 -type f -name '*.py' -exec cp -p {} "$PACKAGE_TARGET/tests/" \;
fi
cp -R "$PACKAGE_TARGET/." "$HOME/.codex/skills/codex-monitor-hook/"

find "$PACKAGE_TARGET/scripts" -maxdepth 1 -type f -name '*.py' -exec cp -p {} "$TARGET_DIR/" \;
chmod 755 "$TARGET_DIR"/*.py
"$PYTHON" "$TARGET_DIR/codex_screen_daemon.py" --self-test
log_elapsed "Runtime copy and self-test"

if [ "$SKIP_CONFIG_UPDATE" -eq 0 ]; then
    "$PYTHON" "$TARGET_DIR/update_codex_config.py" \
        "$CODEX_HOME/config.toml" "$PYTHON" "$TARGET_DIR/codex_hook_relay.py" \
        "$HOOK_PROFILE" posix
    verify_hook_block "$EXPECTED_HOOK_COUNT"
    log_elapsed "Hook config update"
    if [ "$SKIP_HOOK_DIAGNOSTIC" -eq 0 ]; then
        if ! "$PYTHON" "$TARGET_DIR/codex_screen_daemon.py" \
            --diagnose-hooks "$REPO_ROOT" --expected-hook-count "$EXPECTED_HOOK_COUNT" \
            --hook-diagnostic-timeout 3; then
            printf '%s\n' "Warning: Codex Hooks are installed; review trust state after restarting Codex." >&2
        fi
        log_elapsed "Codex hook diagnostic"
    fi
fi

printf '%s\n' "Installed Codex Monitor Hook for macOS."
printf '%s\n' "Python: $PYTHON"
printf '%s\n' "Runtime: $TARGET_DIR"
printf '%s\n' "Config: $CODEX_HOME/config.toml"
printf '%s\n' "Restart Codex to reload config.toml and Hooks."
