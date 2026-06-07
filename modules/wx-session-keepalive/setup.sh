#!/bin/bash
# setup.sh — 微信公众平台会话保活一键部署
#
# 自动检测部署环境（Docker / systemd / 裸机），完成全部配置。
# 部署完成后微信会话将每12小时自动续期，无需人工介入。
#
# 使用方式：
#   bash modules/wx-session-keepalive/setup.sh [选项]
#
# 选项：
#   --docker <container>   Docker 容器模式，指定容器名
#   --systemd <service>    systemd 服务模式，指定服务名
#   --standalone           独立 Python 进程模式
#   --data-dir <path>      数据目录（存放 cookies/token），默认 ./data
#   --auto                 自动检测模式（默认）
#
# 自动检测逻辑：
#   1. 如果当前目录有 docker-compose.yml 或指定了容器名 → Docker 模式
#   2. 如果有 systemd 服务文件 → systemd 模式
#   3. 否则 → 独立进程模式

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODULE_FILE="${SCRIPT_DIR}/wx_session_refresh.py"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# 默认值
MODE="auto"
CONTAINER_NAME=""
SERVICE_NAME=""
DATA_DIR=""
CRON_USER="$(whoami)"

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --docker)
            MODE="docker"
            CONTAINER_NAME="$2"
            shift 2
            ;;
        --systemd)
            MODE="systemd"
            SERVICE_NAME="$2"
            shift 2
            ;;
        --standalone)
            MODE="standalone"
            shift
            ;;
        --data-dir)
            DATA_DIR="$2"
            shift 2
            ;;
        --auto)
            MODE="auto"
            shift
            ;;
        *)
            echo "未知参数: $1"
            exit 1
            ;;
    esac
done

log() {
    echo "[wx-keepalive] $1"
}

# ============================================
# 自动检测部署模式
# ============================================
detect_mode() {
    if [ "$MODE" != "auto" ]; then
        return
    fi

    # 检测 Docker
    if command -v docker &>/dev/null; then
        # 查找运行中的包含 weixin/wechat/werss/wx 关键词的容器
        local containers
        containers=$(docker ps --format "{{.Names}}" 2>/dev/null | grep -iE "we.*rss|wechat|weixin|wx" || true)
        if [ -n "$containers" ]; then
            CONTAINER_NAME=$(echo "$containers" | head -1)
            MODE="docker"
            log "检测到 Docker 容器: ${CONTAINER_NAME}"
            return
        fi

        # 检测 docker-compose
        if [ -f "${PROJECT_DIR}/docker-compose.yml" ] || [ -f "${PROJECT_DIR}/docker-compose.yaml" ]; then
            # 从 compose 文件中找容器
            local compose_containers
            compose_containers=$(docker compose ps --format "{{.Name}}" 2>/dev/null || docker-compose ps --format "{{.Name}}" 2>/dev/null || true)
            if [ -n "$compose_containers" ]; then
                CONTAINER_NAME=$(echo "$compose_containers" | head -1)
                MODE="docker"
                log "检测到 docker-compose 容器: ${CONTAINER_NAME}"
                return
            fi
        fi
    fi

    # 检测 systemd 服务
    if command -v systemctl &>/dev/null; then
        local services
        services=$(systemctl list-units --type=service --state=running 2>/dev/null | grep -iE "we.*rss|wechat|weixin|wx" | awk '{print $1}' || true)
        if [ -n "$services" ]; then
            SERVICE_NAME=$(echo "$services" | head -1 | sed 's/\.service$//')
            MODE="systemd"
            log "检测到 systemd 服务: ${SERVICE_NAME}"
            return
        fi
    fi

    # 默认独立模式
    MODE="standalone"
    log "未检测到 Docker/systemd，使用独立模式"
}

# ============================================
# Docker 模式部署
# ============================================
setup_docker() {
    log "部署模式: Docker (容器: ${CONTAINER_NAME})"

    # 检查容器是否运行
    if ! docker inspect "${CONTAINER_NAME}" &>/dev/null; then
        log "ERROR: 容器 ${CONTAINER_NAME} 不存在"
        exit 1
    fi

    # 检测容器内 Playwright 浏览器路径
    local browsers_path
    browsers_path=$(docker exec "${CONTAINER_NAME}" bash -c 'find / -name "pw_run.sh" 2>/dev/null | head -1 | xargs dirname | xargs dirname' 2>/dev/null || true)
    if [ -z "$browsers_path" ]; then
        log "WARNING: 容器内未找到 Playwright 浏览器，续期脚本可能无法运行"
        log "  请在容器内安装: playwright install"
        browsers_path="/root/.cache/ms-playwright"
    fi
    log "Playwright 浏览器路径: ${browsers_path}"

    # 检测容器内是否有 WeRSS 的 wx.lic（决定用 WeRSS 子类还是通用类）
    local has_werss
    has_werss=$(docker exec "${CONTAINER_NAME}" test -f /app/data/wx.lic && echo "yes" || echo "no")

    # 生成容器内脚本（包装器，设置正确的环境）
    local wrapper_content
    if [ "$has_werss" = "yes" ]; then
        log "检测到 WeRSS 环境，使用 WeRSS 专用模式"
        wrapper_content="#!/usr/bin/env python3
import sys, os
sys.path.insert(0, '/app')
sp = '/app/x86_64/lib/python3.13/site-packages'
if os.path.exists(sp): sys.path.insert(0, sp)
os.chdir('/app')
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '${browsers_path}'
from wx_session_refresh import WeRSSSessionKeepAlive, main
sys.exit(main())
"
    else
        log "使用通用模式"
        wrapper_content="#!/usr/bin/env python3
import sys, os
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '${browsers_path}'
from wx_session_refresh import main
sys.exit(main())
"
    fi

    # 复制模块到容器
    docker cp "${MODULE_FILE}" "${CONTAINER_NAME}:/app/wx_session_refresh.py"
    log "OK: 模块已复制到容器 /app/wx_session_refresh.py"

    # 创建启动包装器
    echo "${wrapper_content}" | docker exec -i "${CONTAINER_NAME}" tee /app/wx-session-refresh.py > /dev/null
    docker exec "${CONTAINER_NAME}" chmod +x /app/wx-session-refresh.py
    log "OK: 启动包装器已创建 /app/wx-session-refresh.py"

    # 配置 crontab
    local cron_cmd="0 */12 * * * docker exec ${CONTAINER_NAME} python3 /app/wx-session-refresh.py >> \${HOME}/wx-session-guard.log 2>&1"
    local cron_exists
    cron_exists=$(crontab -l 2>/dev/null | grep -c "wx-session-refresh" || true)

    if [ "$cron_exists" -eq 0 ]; then
        (crontab -l 2>/dev/null; echo ""; echo "# 微信会话自动续期 - 每12小时"; echo "${cron_cmd}") | crontab -
        log "OK: crontab 已配置"
    else
        log "SKIP: crontab 已存在"
    fi

    # 测试运行
    log "执行首次测试..."
    docker exec "${CONTAINER_NAME}" python3 /app/wx-session-refresh.py 2>&1 || log "WARNING: 首次测试未成功（可能需要先扫码登录）"
}

# ============================================
# systemd 模式部署
# ============================================
setup_systemd() {
    log "部署模式: systemd (服务: ${SERVICE_NAME})"

    # 找到服务的工作目录
    local work_dir
    work_dir=$(systemctl show "${SERVICE_NAME}" -p WorkingDirectory --value 2>/dev/null || echo "")
    if [ -z "$work_dir" ] || [ "$work_dir" = "" ]; then
        work_dir="${PROJECT_DIR}"
    fi
    log "工作目录: ${work_dir}"

    DATA_DIR="${DATA_DIR:-${work_dir}/data}"
    mkdir -p "${DATA_DIR}"

    # 复制模块
    cp "${MODULE_FILE}" "${work_dir}/wx_session_refresh.py"
    log "OK: 模块已复制到 ${work_dir}/wx_session_refresh.py"

    # 生成配置文件
    local config_file="${DATA_DIR}/wx-keepalive-config.yaml"
    cat > "${config_file}" << YAML
cookie_file: ${DATA_DIR}/wx_cookies.json
token_file: ${DATA_DIR}/wx_token.yaml
browser_type: ${BROWSER_TYPE:-webkit}
YAML
    log "OK: 配置文件已生成 ${config_file}"

    # 配置 crontab
    local cron_cmd="0 */12 * * * cd ${work_dir} && python3 wx_session_refresh.py --config ${config_file} >> \${HOME}/wx-session-guard.log 2>&1"
    local cron_exists
    cron_exists=$(crontab -l 2>/dev/null | grep -c "wx-session-refresh\|wx_session_refresh" || true)

    if [ "$cron_exists" -eq 0 ]; then
        (crontab -l 2>/dev/null; echo ""; echo "# 微信会话自动续期 - 每12小时"; echo "${cron_cmd}") | crontab -
        log "OK: crontab 已配置"
    else
        log "SKIP: crontab 已存在"
    fi

    log "注意: 首次使用前需要先完成微信扫码登录，cookies 会保存到 ${DATA_DIR}/"
}

# ============================================
# 独立模式部署
# ============================================
setup_standalone() {
    log "部署模式: 独立进程"

    DATA_DIR="${DATA_DIR:-${PROJECT_DIR}/data}"
    mkdir -p "${DATA_DIR}"

    # 复制模块
    local target_dir="${PROJECT_DIR}"
    cp "${MODULE_FILE}" "${target_dir}/wx_session_refresh.py"
    log "OK: 模块已复制到 ${target_dir}/wx_session_refresh.py"

    # 生成配置文件
    local config_file="${DATA_DIR}/wx-keepalive-config.yaml"
    cat > "${config_file}" << YAML
cookie_file: ${DATA_DIR}/wx_cookies.json
token_file: ${DATA_DIR}/wx_token.yaml
browser_type: ${BROWSER_TYPE:-webkit}
YAML
    log "OK: 配置文件已生成 ${config_file}"

    # 检查 Playwright 是否安装
    if python3 -c "from playwright.sync_api import sync_playwright" 2>/dev/null; then
        log "OK: Playwright 已安装"
    else
        log "WARNING: Playwright 未安装，请执行: pip install playwright && playwright install webkit"
    fi

    # 配置 crontab
    local cron_cmd="0 */12 * * * cd ${target_dir} && python3 wx_session_refresh.py --config ${config_file} >> \${HOME}/wx-session-guard.log 2>&1"
    local cron_exists
    cron_exists=$(crontab -l 2>/dev/null | grep -c "wx-session-refresh\|wx_session_refresh" || true)

    if [ "$cron_exists" -eq 0 ]; then
        (crontab -l 2>/dev/null; echo ""; echo "# 微信会话自动续期 - 每12小时"; echo "${cron_cmd}") | crontab -
        log "OK: crontab 已配置"
    else
        log "SKIP: crontab 已存在"
    fi

    log "注意: 首次使用前需要先完成微信扫码登录，cookies 会保存到 ${DATA_DIR}/"
}

# ============================================
# 主流程
# ============================================
main() {
    log "========================================="
    log "  微信会话保活 - 自动部署"
    log "========================================="

    # 检查模块文件
    if [ ! -f "${MODULE_FILE}" ]; then
        log "ERROR: 模块文件不存在: ${MODULE_FILE}"
        exit 1
    fi

    # 检测或使用指定模式
    detect_mode

    case "$MODE" in
        docker)
            setup_docker
            ;;
        systemd)
            setup_systemd
            ;;
        standalone)
            setup_standalone
            ;;
        *)
            log "ERROR: 未知模式 ${MODE}"
            exit 1
            ;;
    esac

    log ""
    log "========================================="
    log "  部署完成"
    log "========================================="
    log ""
    log "模式: ${MODE}"
    log "定时任务: 每12小时自动续期"
    log "日志: ~/wx-session-guard.log"
    log ""
    log "微信 slave_sid 有效期4天，12小时刷新一次 = 8倍安全余量"
    log "唯一需要人工的场景: 微信服务端强制下线（约30天一次）"
}

main "$@"
