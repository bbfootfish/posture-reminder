#!/bin/bash
# 生成坐姿提醒话术 MP3（edge-tts + 500ms 前导填充）
#
# 用法：bash gen_phrases_edge.sh [输出目录] [音色]
#   音色默认 zh-CN-YunxiaNeural（云夏，童声可爱，孩子接受度高）
#   可选：zh-CN-YunxiNeural（云希 活泼）、zh-CN-YunyangNeural（云扬 播音）、
#         zh-CN-YunjianNeural（云健 激昂）
#
# 两个必须注意的点：
#   1. 前导 500ms 低音量填充 —— DLNA 渲染器启动时丢弃开头 0.4-0.5s，
#      不加填充会吞掉第一个字。
#   2. edge-tts 输出 24kHz，必须重采样到 44.1kHz 单声道，否则部分音箱解码异常。
set -euo pipefail

OUT="${1:-out}"
VOICE="${2:-zh-CN-YunxiaNeural}"
PAD_SEC="0.5"
PAD_VOL="0.02"

command -v ffmpeg >/dev/null || { echo "需要 ffmpeg"; exit 1; }
python3 -c "import edge_tts" 2>/dev/null || { echo "需要 pip install edge-tts"; exit 1; }

mkdir -p "$OUT"
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT

ffmpeg -y -loglevel error -f lavfi -i "anullsrc=r=44100:cl=mono" \
  -t "$PAD_SEC" -af "volume=$PAD_VOL" "$work/pad.aiff"

gen() {
  local name="$1" text="$2"
  edge-tts --voice "$VOICE" --rate=-5% --text "$text" --write-media "$work/$name.raw.mp3"
  ffmpeg -y -loglevel error -i "$work/pad.aiff" -i "$work/$name.raw.mp3" \
    -filter_complex "[0:a][1:a]concat=n=2:v=0:a=1[out]" -map "[out]" \
    -ar 44100 -ac 1 -b:a 128k "$OUT/$name.mp3"
  printf "%-18s %s\n" "$name.mp3" "$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$OUT/$name.mp3")s"
}

gen too_close_1 "离本子远一点点"
gen too_close_2 "往后退一点点"
gen head_down_1 "头抬高一点点"
gen head_down_2 "把背直起来一点点"
gen lean_1 "坐正一下"
gen lean_2 "两边坐平"

echo "done -> $OUT/  (voice=$VOICE)"
