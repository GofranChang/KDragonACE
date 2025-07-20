#!/bin/bash

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PLUGIN_FILE="$SCRIPT_DIR/extras/ace.py"

# 提示用户输入 Klipper 安装路径
echo "🔧 请输入你的 Klipper 安装路径 [默认: ~/klipper]:"
echo "🔧 Please enter your Klipper install path [Default: ~/klipper]:"
read -r USER_INPUT

if [ -z "$USER_INPUT" ]; then
  KLIPPER_PATH="$HOME/klipper"
else
  KLIPPER_PATH="$USER_INPUT"
fi

# 去除路径尾部可能多余的斜杠
KLIPPER_PATH="${KLIPPER_PATH%/}"

# 检查目录是否存在
if [ ! -d "$KLIPPER_PATH/klippy/extras" ]; then
  echo "❌ 错误：找不到目录 $KLIPPER_PATH/klippy/extras"
  echo "❌ Error: Directory $KLIPPER_PATH/klippy/extras not found."
  exit 1
fi

# 拷贝插件文件
~/klippy-env/bin/pip install --upgrade pyserial==3.5
echo "📄 正在复制 $PLUGIN_FILE 到 $KLIPPER_PATH/klippy/extras ..."
echo "📄 Copying $PLUGIN_FILE to $KLIPPER_PATH/klippy/extras ..."
cp "$PLUGIN_FILE" "$KLIPPER_PATH/klippy/extras/"

# 安装配置文件（假设是复制 firmware 目录到打印机配置）
CONFIG_TARGET="$HOME/printer_data/config/ace_mmu"

echo "📂 正在复制固件配置文件到 $CONFIG_TARGET ..."
mkdir -p "$CONFIG_TARGET"
cp -r "$SCRIPT_DIR/firmware/"* "$CONFIG_TARGET"

echo "✅ 插件安装完成！"
echo "✅ Plugin installation complete!"
echo "🔁 请重启 Klipper 以加载新插件："
echo "🔁 Please restart Klipper to load the plugin:"
echo "   sudo systemctl restart klipper"
