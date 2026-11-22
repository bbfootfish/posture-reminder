#!/bin/bash
# 生成坐姿提醒话术 MP3（含 DLNA 前导填充音）
#
# 为什么需要前导填充：小爱音箱等 DLNA 渲染器在音频管道启动时会丢弃开头
# 约 0.4-0.5 秒，直接播放会吞掉第一个字（"往后退一点点"变成"后退一点点"）。
# 在话术前加 0.5 秒低音量填充音后，被丢弃的是填充音，话术完整。
#
# 用法：在 macOS 上执行本脚本，然后把 final/*.mp3 拷到容器的 /opt/posture-media/
set -euo pipefail

VOICE="Tingting"        # macOS 中文语音
PAD_MS="0.5"            # 前导填充时长（实测 500ms 最佳）
PAD_VOLUME="0.02"       # 低音量而非绝对静音，避免播放器跳过静音段
OUT="${1:-final}"

mkdir -p "$OUT"
workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

# 语音文本 → 文件名（与 posture/phrases.py 的 PHRASES 保持一致）
declare -a ITEMS=(
  "too_close_1:离本子远一点点"
  "too_close_2:往后退一点点"
  "head_down_1:头抬高一点点"
  "head_down_2:把背直起来一点点"
  "lean_1:坐正一下"
  "lean_2:两边坐平"
)

ffmpeg -y -loglevel error -f lavfi -i "anullsrc=r=44100:cl=mono" \
  -t "$PAD_MS" -af "volume=$PAD_VOLUME" "$workdir/pad.aiff"

for item in "${ITEMS[@]}"; do
  name="${item%%:*}"
  text="${item#*:}"
  say -v "$VOICE" -o "$workdir/$name.aiff" "$text"
  ffmpeg -y -loglevel error -i "$workdir/pad.aiff" -i "$workdir/$name.aiff" \
    -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1[out]" -map "[out]" \
    -ar 44100 -ac 1 -b:a 128k "$OUT/$name.mp3"
  printf "%-18s %ss\n" "$name.mp3" "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/$name.mp3")"
done

echo "done -> $OUT/"
