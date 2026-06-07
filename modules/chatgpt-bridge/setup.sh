#!/bin/bash
#
# ChatGPT Bridge 部署脚本
# 部署 ChatGPT Bridge 服务 + Session 保活 cron
#
# 用法:
#   bash setup.sh                    # 自动部署
#   bash setup.sh --init             # 首次部署（含登录）
#   bash setup.sh --port 9018        # 指定端口
#   bash setup.sh --data-dir /path   # 指定数据目录

set -e

# 默认配置
BRIDGE_PORT="${BRIDGE_PORT:-9019}"
BRIDGE_HOST="127.0.0.1"
DATA_DIR="/home/deploy/data/chatgpt-bridge"
MODULE_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="flyranking-chatgpt-bridge"
DEPLOY_DIR="/home/deploy/apps/chatgpt-bridge"
DO_INIT=false

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        --init) DO_INIT=true; shift ;;
        --port) BRIDGE_PORT="$2"; shift 2 ;;
        --data-dir) DATA_DIR="$2"; shift 2 ;;
        *) shift ;;
    esac
done

echo "======================================"
echo "ChatGPT Bridge 部署"
echo "======================================"
echo "端口: $BRIDGE_PORT"
echo "数据目录: $DATA_DIR"
echo "模块目录: $MODULE_DIR"
echo ""

# 1. 检查依赖
echo "[1/6] 检查依赖..."
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi

# 检查 playwright
if ! python3 -c "import playwright" 2>/dev/null; then
    echo "Installing playwright..."
    pip3 install playwright pyyaml -q
    python3 -m playwright install chromium
fi

echo "  依赖检查通过"

# 2. 创建数据目录
echo "[2/6] 创建数据目录..."
mkdir -p "$DATA_DIR"
mkdir -p "$DEPLOY_DIR"
echo "  $DATA_DIR OK"

# 3. 部署文件
echo "[3/6] 部署文件..."
cp "$MODULE_DIR/chatgpt_bridge.py" "$DEPLOY_DIR/"
cp "$MODULE_DIR/chatgpt_session.py" "$DEPLOY_DIR/"
cp "$MODULE_DIR/config.example.yaml" "$DEPLOY_DIR/"

# 创建配置文件
cat > "$DEPLOY_DIR/config.yaml" <<YAML
data_dir: $DATA_DIR
token_file: $DATA_DIR/chatgpt_token.json
cookies_file: $DATA_DIR/chatgpt_cookies.json
port: $BRIDGE_PORT
host: $BRIDGE_HOST
YAML

echo "  文件部署到 $DEPLOY_DIR"

# 4. 创建 systemd 服务
echo "[4/6] 创建 systemd 服务..."
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null <<EOF
[Unit]
Description=ChatGPT Bridge - Pro Subscription to API
After=network.target

[Service]
Type=simple
User=deploy
WorkingDirectory=$DEPLOY_DIR
ExecStart=/usr/bin/python3 $DEPLOY_DIR/chatgpt_bridge.py --port $BRIDGE_PORT --host $BRIDGE_HOST --token-file $DATA_DIR/chatgpt_token.json
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
echo "  systemd 服务创建完成"

# 5. 配置 session 保活 cron
echo "[5/6] 配置 Session 保活..."
CRON_CMD="python3 $DEPLOY_DIR/chatgpt_session.py --refresh --config $DEPLOY_DIR/config.yaml >> $DATA_DIR/session-refresh.log 2>&1"
CRON_ENTRY="0 */6 * * * $CRON_CMD"

# 检查是否已有 cron
CURRENT_CRON=$(crontab -l 2>/dev/null || true)
if echo "$CURRENT_CRON" | grep -q "chatgpt_session"; then
    echo "  Cron 已存在，更新..."
    CURRENT_CRON=$(echo "$CURRENT_CRON" | grep -v "chatgpt_session")
fi
echo "$CURRENT_CRON
# ChatGPT Session 保活（每6小时刷新一次）
$CRON_ENTRY" | crontab -
echo "  Cron 配置完成（每6小时刷新）"

# 6. 首次登录（可选）
if [ "$DO_INIT" = true ]; then
    echo "[6/6] 首次登录..."
    echo "即将打开浏览器，请在浏览器中登录 ChatGPT..."
    python3 "$DEPLOY_DIR/chatgpt_session.py" --init --config "$DEPLOY_DIR/config.yaml"
else
    echo "[6/6] 跳过首次登录"
    echo ""
    echo "======================================"
    echo "部署完成！后续步骤："
    echo "======================================"
    echo ""
    echo "1. 首次登录获取 token（在有浏览器的机器上执行）："
    echo "   python3 $DEPLOY_DIR/chatgpt_session.py --init --config $DEPLOY_DIR/config.yaml"
    echo ""
    echo "   或者手动设置 token："
    echo "   curl -X POST http://127.0.0.1:$BRIDGE_PORT/v1/token \\"
    echo "     -H 'Content-Type: application/json' \\"
    echo "     -d '{\"access_token\": \"YOUR_TOKEN_HERE\"}'"
    echo ""
    echo "   获取 token 的方法："
    echo "   a) 登录 chatgpt.com"
    echo "   b) 打开 https://chatgpt.com/api/auth/session"
    echo "   c) 复制 accessToken 字段的值"
    echo ""
    echo "2. 启动服务："
    echo "   sudo systemctl start $SERVICE_NAME"
    echo ""
    echo "3. 验证："
    echo "   curl http://127.0.0.1:$BRIDGE_PORT/health"
    echo ""
    echo "4. LLM Gateway 会自动识别 ChatGPT Bridge（默认 127.0.0.1:9019）"
    echo "   如需自定义：设置环境变量 CHATGPT_BRIDGE_URL=http://127.0.0.1:$BRIDGE_PORT"
fi

# 启动服务
sudo systemctl start "$SERVICE_NAME" 2>/dev/null || true
sleep 1

# 检查状态
if systemctl is-active --quiet "$SERVICE_NAME"; then
    echo ""
    echo "服务状态: ACTIVE"
    curl -s "http://127.0.0.1:$BRIDGE_PORT/health" 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "(等待服务启动...)"
else
    echo ""
    echo "服务状态: 未启动（需要先设置 token）"
fi
