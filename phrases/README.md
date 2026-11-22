# 话术文件

运行时不做 TTS。6 条话术由家长在本地预生成，文件名必须与下表一致，
放到 PVE 的 `/opt/posture-media/`，由静态 HTTP 服务提供。

| 类别 | 变体 | 文本 | 文件名 |
|---|---|---|---|
| `too_close` | 0 | 离本子远一点点 | `too_close_1.mp3` |
| `too_close` | 1 | 往后退一点点 | `too_close_2.mp3` |
| `head_down` | 0 | 头抬高一点点 | `head_down_1.mp3` |
| `head_down` | 1 | 把背直起来一点点 | `head_down_2.mp3` |
| `lean` | 0 | 坐正一下 | `lean_1.mp3` |
| `lean` | 1 | 两边坐平 | `lean_2.mp3` |

要求：

- MP3，单声道即可，响度偏低（“轻声说一句话”）；
- 不要出现“你又驼背了”“怎么不听话”这类评价性内容；
- 文件名区分大小写，URL 会被 `urllib.parse.quote` 编码。

服务从 `POSTURE_MEDIA_BASE_URL` 拼出 `http://<host>:8090/<文件名>`，
再把 URL 交给 `SPEAK_CMD`。例如：

```bash
POSTURE_MEDIA_BASE_URL=http://192.168.2.130:8090
SPEAK_CMD=python /app/tools/speak.py --class {class} --variant {variant} --url {url} --volume 35
```
