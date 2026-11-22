# 坐姿语音提醒服务（posture-reminder）

在 PVE 上常驻运行：从一路固定侧视 RTSP 画面判断孩子写作业时是否长期坐姿偏离，
持续偏离时让书房的 DLNA 音箱轻声说一句固定话术。不评价人、不记录图像、不做 UI。

> 阈值来自 2026-11-13 孩子本人配合的 30 秒实测，属于**初步值**，试点一周后按误报再调。

## 它做什么

- 以 5 fps 消费 `POSTURE_RTSP_URL`，丢弃刚连上时的灰帧；
- 计算三个侧视指标：`s_norm`、`head_up`、`head_fwd`；
- 两道物理闸门先过滤假姿态：肩宽 ≥ 画面 6%、`head_up ≥ 0.03`（空椅子上的假检测会被丢弃）；
- 帧间运动闸门：最近 15 秒几乎无画面变化时视为无人，不判定、不播报；
- 3 秒滚动中位数平滑，最近 20 秒越线占比 ≥ 55% 且当前越线才提醒；
- 同类问题冷却：调试阶段不限间隔，正式使用至少 300 秒（5 分钟）；每小时上限默认不限制；
- 时段规则（三态）：
  - **不检测**：每日 `00:00-08:00`，以及上学日 `08:00-18:00`（连推理一起停，不写样本）；
  - **只记录不播报**：每日 `22:00-24:00`；
  - **全功能**：其余时段。
  - 「上学日」= 法定工作日（自动含调休补班），再排除寒暑假，见 `data/school_breaks.json`；
  - 人工校准与机位体检**不受**时段限制；
- 提供 `/healthz`、`POST /calibrate`、`POST /audit`、网页看板与标注接口，
  提醒事件写 SQLite（只有时间、类别、指标）。

## 它不做什么

- 不做人脸识别，不判断“这是谁”；
- 不存视频、不上传任何图像；报警截图可选、仅本机、25 小时后自动删除；
- 不实现米家/私有协议，不碰小米账号；
- 不做 App、不做家长周报（网页看板仅本机局域网只读，另含人工标注）；
- 不控制相机云台，不使用 GPU。

## 目录

```
posture/
├── posture/              # 服务包
│   ├── service.py        # 采集主循环、校准/体检编排、健康状态
│   ├── state_machine.py  # 平滑、20 秒窗口、冷却、整点上限
│   ├── metrics.py        # MediaPipe 关键点 -> 三个指标
│   ├── judge.py          # 越线判定与类别
│   ├── audit.py          # 机位体检
│   ├── api.py            # /healthz /calibrate /audit
│   ├── replay.py         # 指标 CSV 回放
│   └── ...
├── tests/                # 不依赖摄像头/GPU/网络的单元测试
├── tools/audit.py        # 命令行机位体检
├── tools/speak.py        # 参考 DLNA 推送脚本（未在真机验证）
├── tools/download_model.py
├── phrases/              # 6 条话术的说明与文件名约定
├── sample_metrics.csv    # 实测姿势的回放样本
├── docs/design.md
├── Dockerfile
└── docker-compose.yml
```

## 部署前提

- PVE 宿主机 `192.168.2.130` 上新建容器，网桥能访问 `192.168.2.0/24`。
- **不要部署到 `192.168.100.50` 的 superagent 容器**：那个网段到不了相机。
- 相机 `192.168.2.133` 已经由 go2rtc 1.9.15 桥接出 **1920×1080 / HEVC / 20 fps** 的 RTSP。
- 机位固定在书桌正侧方，机位移动后必须重新体检和校准。
- 书房 DLNA 音箱是 `小爱音箱-6646`（`192.168.2.167`）；`XiaoAiTongXueX08A` 不可用。

## 快速开始

```bash
cd posture
cp .env.example .env        # 填 RTSP 地址和媒体地址
docker compose build        # 会下载 pose_landmarker_full.task
docker compose up -d
curl http://192.168.2.130:8080/healthz
```

服务启动后**先做机位体检（约 20 秒），再做校准（约 2 秒）**：

```bash
# 1) 20 秒机位体检；不通过时服务不会播报
curl -X POST http://192.168.2.130:8080/audit

# 2) 让孩子按平时写字的姿势坐着，采集 2 秒（约 40 帧）取中位数
curl -X POST http://192.168.2.130:8080/calibrate
```

校准必须在**孩子正常写字时**做，不能要求她“坐得笔直”：
实测笔直坐 `head_up≈0.78`，正常写字 `≈0.38`，用前者做基线会把所有正常写字判成低头。

体检通过且校准成功后，`/healthz` 里的 `camera_audit_passed` 为 `true`，
`state` 会随姿势变化为 `ok` / `out_of_line` / `cooldown` 等。

孩子通常会比服务晚坐到书桌前：服务启动后如果还没做过体检，会在检测到**有效坐姿
（可见姿态且 `head_up > 0`）持续 10 秒**后自动跑一次 `/audit`；失败则 10 分钟后
再试（可用 `POSTURE_AUTO_AUDIT_*` 调整）。手动 `POST /audit` 任何时候都可以强制重跑。
`/calibrate` 仍建议由家长在孩子正常写字时手动执行，避免把非写字姿态当基线。

## 音频与话术

运行时不做 TTS。6 条固定话术预生成 MP3，由静态 HTTP 服务提供。

| 类别 | 话术 | 文件 |
|---|---|---|
| `too_close` | 离本子远一点点 / 往后退一点点 | `too_close_1.mp3` / `too_close_2.mp3` |
| `head_down` | 头抬高一点点 / 把背直起来一点点 | `head_down_1.mp3` / `head_down_2.mp3` |
| `lean` | 坐正一下 / 两边坐平 | `lean_1.mp3` / `lean_2.mp3` |

### 音色与生成方式

生产音色为 **云夏（`zh-CN-YunxiaNeural`）**——edge-tts 的童声男声，语气轻快可爱，
孩子接受度高。可选其他中文男声：

| 音色 | 风格 | 适用 |
|---|---|---|
| `zh-CN-YunxiaNeural` | 童声、可爱 | **当前生产选择** |
| `zh-CN-YunxiNeural` | 活泼、阳光 | 想更明快可选 |
| `zh-CN-YunyangNeural` | 专业播报 | 想更端正可选 |
| `zh-CN-YunjianNeural` | 激昂解说 | 冲击力偏强，不建议 |

> **注意**：不使用任何真人明星的声音。模仿/克隆真人音色涉及人格权（民法典第 1023 条
> 对自然人声音的保护，参照肖像权），本项目只用厂商预训练且授权明晰的合成音色。

用 `tools/gen_phrases_edge.sh`（需容器内 `edge-tts` + `ffmpeg`）：

```bash
bash tools/gen_phrases_edge.sh out zh-CN-YunxiaNeural
```

无 ffmpeg 可用 `pip install imageio-ffmpeg` 提供静态二进制。
macOS 备选方案为 `tools/gen_phrases.sh`（系统 `say` + ffmpeg，音色为 Tingting）。

### 话术必须带 0.5 秒前导填充音（重要）

小爱音箱等 DLNA 渲染器在音频管道启动时会**丢弃开头约 0.4–0.5 秒**，
直接播放会吞掉第一个字（实测「往后退一点点」只听到「后退一点点」）。
因此在每条话术前拼接 **500 ms 低音量填充**（`volume=0.02`，低音量而非绝对静音，
避免播放器对静音段做跳过优化），被丢弃的落在填充音上，话术即完整。

各文件时长约 2.4–3.1 秒（含 0.5 s 填充）。实测 500 ms 最佳；800/1000 ms 会显拖沓。

### 媒体服务（跑在 PVE 宿主，不在容器）

媒体服务**必须跑在 PVE 宿主机**，不能放在容器内——否则容器重建或 IP 变化会导致
音箱拉不到音频。宿主 IP 固定为 `192.168.2.130`。

```ini
# /etc/systemd/system/posture-media-host.service（PVE 宿主）
[Service]
WorkingDirectory=/opt/posture-media
ExecStart=/usr/bin/python3 -m http.server 8090 --bind 0.0.0.0
Restart=always
```

容器通过 **LXC bind mount** 共享同一份音频目录，改话术只需改一处：

```bash
# PVE 宿主执行（需短暂停机）
pct set 151 -mp0 /opt/posture-media,mp=/opt/posture-media
```

`POSTURE_MEDIA_BASE_URL` 指向宿主：`http://192.168.2.130:8090`。
**不能**用 `127.0.0.1`（音箱访问不到），也**不要**用容器 IP（容器重建即失效）。

### DLNA 播报配置

实测有效的配置（写入 `EnvironmentFile`，见下方「配置」一节的空格陷阱）：

```bash
POSTURE_MEDIA_BASE_URL=http://192.168.2.140:8090
SPEAK_CMD=/opt/posture-pr2/.venv/bin/python /opt/posture-pr2/tools/speak.py \
  --url {url} \
  --control-url http://192.168.2.167:9999/AVTransport/control \
  --volume-control-url http://192.168.2.167:9999/RenderingControl/control \
  --volume 35 --timeout 8
```

- `{class}`、`{variant}`、`{url}`、`{text}` 四个占位符；命令不经过 shell。
- 直接写 `--control-url` 比每次 SSDP 发现快约 5 秒，适合固定音箱。
- 音箱 IP 走 DHCP，**建议在路由器做静态绑定**，否则换 IP 后播报失效。
- 若用 SSDP 自动发现，`--name 小爱` 可按设备名匹配（需 `tools/speak.py` 的
  `_descendant_text` 修复，friendlyName 嵌在 `<device>` 里，浅查找会失败）。

### 已知硬件限制

- 小爱音箱的 `GetPositionInfo` 恒返回 `RelTime=00:00:00`，**不能靠它判断是否真的出声**，
  只能凭人耳确认。
- 该音箱只开放 9999（DLNA），MIoT 局域网接口关闭，因此无法走 mlocal/TTS 直说。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `POSTURE_RTSP_URL` | 空 | 必填，RTSP over TCP |
| `POSTURE_MODEL_PATH` | `models/pose_landmarker_full.task` | 容器内为 `/app/models/...` |
| `POSTURE_DATA_DIR` | `data` | `baseline.json` 与 SQLite 所在目录 |
| `POSTURE_TARGET_FPS` | `5` | 判定采样率 |
| `POSTURE_NIGHT_BLOCK` | `00:00-08:00` | 每日硬屏蔽，连推理一起停 |
| `POSTURE_SCHOOL_BLOCK` | `08:00-18:00` | 仅上学日生效 |
| `POSTURE_LATE_NIGHT_BLOCK` | `22:00-24:00` | 软屏蔽：检测记录但不播报 |
| `POSTURE_SCHOOL_BREAKS` | `data/school_breaks.json` | 寒暑假区间文件 |
| `POSTURE_MEDIA_BASE_URL` | `http://192.168.2.130:8090` | 静态 MP3 地址 |
| `SPEAK_CMD` | 空 | 空 = dry-run |
| `POSTURE_COOLDOWN_SECONDS` | `300` | 同类冷却；正式模式强制 ≥300 |
| `POSTURE_DEBUG_MODE` | `0` | `1`＝调试，忽略冷却间隔便于观察 |
| `POSTURE_HOURLY_CAP` | `0` | 每小时上限；`0` 或负数＝不限制 |
| `POSTURE_MOTION_GATE` | `1` | 帧间运动闸门（无人画面不判定） |
| `POSTURE_MOTION_WINDOW_SECONDS` | `15` | 运动判定的回看窗口 |
| `POSTURE_MOTION_MIN_MOVING` | `2` | 窗口内至少多少帧检测到运动 |
| `POSTURE_OCCUPANCY_THRESHOLD` | `0.55` | 20 秒窗口越线占比 |
| `POSTURE_REQUIRE_AUDIT` | `1` | 未通过体检不播报 |
| `POSTURE_AUTO_AUDIT` | `1` | 检测到坐姿后自动体检 |
| `POSTURE_AUTO_AUDIT_PRESENCE_SECONDS` | `10` | 有效坐姿持续多久后自动体检 |
| `POSTURE_AUTO_AUDIT_RETRY_SECONDS` | `600` | 自动体检失败后的重试间隔 |
| `POSTURE_API_PORT` | `8080` | 容器内 API 端口 |

完整列表见 `posture/config.py`；路径均相对于运行目录 `posture/`。

### ⚠️ systemd 配置陷阱：含空格的变量必须用 EnvironmentFile

`SPEAK_CMD` 的值包含空格（命令 + 参数）。写在 unit 的 `Environment=` 里会被
systemd 按空格拆成多个变量，服务实际只拿到第一个词：

```ini
# 错误 —— 服务只拿到 "/opt/posture-pr2/.venv/bin/python"
Environment=SPEAK_CMD=/opt/posture-pr2/.venv/bin/python /opt/posture-pr2/tools/speak.py --url {url}
```

正确做法是写入独立文件并引用：

```ini
# /etc/systemd/system/posture-pr2.service
EnvironmentFile=/opt/posture-pr2/service.env
```

```
# /opt/posture-pr2/service.env
POSTURE_MEDIA_BASE_URL=http://192.168.2.140:8090
SPEAK_CMD=/opt/posture-pr2/.venv/bin/python /opt/posture-pr2/tools/speak.py --url {url} --control-url http://192.168.2.167:9999/AVTransport/control --volume-control-url http://192.168.2.167:9999/RenderingControl/control --volume 35 --timeout 8
```

改完必须 `systemctl daemon-reload && systemctl restart posture-pr2`。

## 时段与日历维护

| 事项 | 频率 | 做法 |
|---|---|---|
| 寒暑假区间 | 每学期一次（1 月 / 7 月） | 编辑 `data/school_breaks.json`，起止日期均含在内，改完重启 |
| 时段本身 | 按需 | 改 `service.env` 的 `POSTURE_NIGHT_BLOCK` / `POSTURE_SCHOOL_BLOCK` / `POSTURE_LATE_NIGHT_BLOCK` |
| 节假日日历 | **2027 年内必须做一次** | `pip install -U chinese-calendar`，否则 2028 年起工作日白天会一直不检测 |

`chinese-calendar` 只包含 2004-2027 的国务院节假日安排。越界年份按「上学日」处理
（fail-closed），即白天继续屏蔽 —— **宁可少测，也不在课堂上开测**。
过期时 `/healthz` 的 `calendar_ok` 为 `false`，看板会显示红色提示，日志按天只告警一次。

`data/school_breaks.json` 格式（缺失或写坏只会告警并按「无寒暑假」运行，不会阻止启动）：

```json
{
  "breaks": [
    {"start": "2027-01-18", "end": "2027-02-20", "label": "寒假"},
    {"start": "2027-07-01", "end": "2027-08-31", "label": "暑假"}
  ]
}
```

## 测试与回放

```bash
# 容器内跑全部单元测试（不依赖摄像头/GPU/网络）
docker compose run --rm app pytest -q

# 或者本机虚拟环境
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pytest -q

# 用实测姿势序列回放状态机
.venv/bin/python -m posture.replay --csv sample_metrics.csv
# 期望：坐正/写字段 0 次，趴桌段 1 次提醒
```

测试覆盖工单验收要求的六条状态机断言，以及指标、校准、体检、HTTP、话术命令。

## 标注纪律（阈值调参的数据前提）

调参建议完全依赖人工标注的质量。**标注错了，阈值就会往错的方向调**——2026-11-20
曾出现 35 条误报全部来自空椅子假姿态，若直接采纳会把阈值调得越来越严，
真人在座的正常姿势反而被漏判。因此标注必须遵守以下规则。

### 三种判定与一种样本

| 类型 | 含义 | 是否参与调参 |
|---|---|---|
| 正报 | 当时确实姿势不好，提醒正确 | 是 |
| 误报 | 孩子在场且坐得挺好，不该提醒 | 是 |
| 作废 | 当时画面里根本没人（空椅子等无效事件） | **否**，仅作数据清理 |
| 漏报样本 | 当时姿势不好但**没有**提醒 | 是，方向与误报相反 |

### 必须遵守的四条

1. **只在真实在座场景标注**。相机恢复、运动闸门生效后，孩子确实坐在书桌前时
   产生的提醒才标正报/误报。空椅子、无人时段一律标「作废」，不要标误报。
2. **每类至少 5 条才开始调参**。代码中 `MIN_SAMPLES = 5`，不足时 `/api/tuning`
   明确返回「暂不建议调整」，不会给出数字。有效 20 条以上置信度才升为 high。
3. **漏报要主动补**。漏报是最容易漏标的类别（人不会去翻没报警的时刻），
   用看板的「回看时间线」点选那一时刻直接记录，不要凭记忆手填时间。
4. **调参不等于上线**。建议须人工审阅后点「应用并重启」才写入
   `data/thresholds_override.json`；服务不会自动改阈值。

### 节奏

- 试点第一周只采集、只标注，不调参；
- 一周后若每类 ≥ 5 条，看 `/api/tuning` 的建议并小步调整（单次不超过
  代码里的 `MAX_HEAD_UP_STEP` / `MAX_FWD_DROP_STEP`）；
- 每 2 周重新校准基线（会长高、椅子会调），校准在**正常写字**时进行，
  不是在「让她坐笔直」时。

### 回看时间线怎么用

看板「回看时间线」按 5 分钟一段展示 24 小时越线轨迹：

- 灰色柱＝该段多数时间无人；绿色＝正常；橙色＝有越线但未达播报条件；
  红色＝越线占比 ≥55%；
- 点选任意一段，显示该段的在场率、越线率、抬头/夹角均值；
- 若判定「这一段姿势确实不好却没提醒」，选好类别点「记为漏报」，
  该段的指标均值会一并存入，作为放宽阈值的依据。

## 健康检查

```bash
curl -s http://192.168.2.130:8080/healthz | python3 -m json.tool
```

主要字段：

- `status`：`ok` / `degraded`（HTTP 始终 200，容器健康检查只看 200）；
- `fps`：最近 5 秒实际推理帧率；
- `person`：最近 5 秒内是否检到姿态；
- `state`：`ok` / `out_of_line` / `cooldown` / `hourly_cap` / `no_person` /
  `outside_hours` / `audit_required` / `no_baseline` / `stream_down` 等；
- `reminders_today`：今日已播报次数；
- `camera_audit_passed`：机位体检是否通过（未做过为 `null`）；
- `baseline_present`、`baseline`：校准基线；
- `motion_gate_enabled`、`motion_active`：运动闸门是否启用、当前画面是否有活动；
- `speaking_enabled`：`SPEAK_CMD` 是否已配置（`false` 时只写日志不发声）。

## 隐私

- 只把 MediaPipe 关键点算成三个数；图像帧在内存中处理完即丢；
- SQLite 只有时间、类别、指标快照，没有图像、没有文件名、没有身份；
- 不连接任何云端服务；模型和依赖在构建镜像时下载。

## 未验证 / 已知限制

- 本仓库开发环境没有 Docker，`docker compose build/up` 与容器内 `/healthz` 未在此环境执行；
  本机已用同一份 Python 代码跑通全部单元测试、回放和 HTTP 健康检查。
- C700 经 go2rtc 的长期稳定性、首帧时间未做长时间测试。
- MediaPipe 在真实儿童画面上的检出率需要按体检脚本实测；仓库只用实测指标回放。
- DLNA 音箱的实际延迟、成功率、`tools/speak.py` 的 SSDP/SOAP 兼容性未在硬件上验证。
- `too_close` 的类别判定使用 `s_norm ≥ 1.15 × 基线` 区分“前塌靠近”与“侧歪”，
  这是工单未给出阈值时的初始启发式，试点时可按实际误报调整。
