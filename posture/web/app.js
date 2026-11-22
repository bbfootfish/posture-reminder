"use strict";

/* ------------------------------------------------------------------ */
/* Small helpers                                                       */
/* ------------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);

const ISSUE_LABELS = {
  head_down: "低头",
  lean: "前倾 / 侧歪",
  too_close: "离桌面太近",
};

const STATE_INFO = {
  ok: { label: "坐姿正常", tone: "ok" },
  out_of_line: { label: "姿势偏离", tone: "warn" },
  warming: { label: "窗口采集中", tone: "warn" },
  cooldown: { label: "同类冷却中", tone: "warn" },
  hourly_cap: { label: "本小时已满", tone: "warn" },
  reminded: { label: "已播报", tone: "warn" },
  reminder_blocked: { label: "播报被禁止", tone: "bad" },
  no_person: { label: "未检测到人", tone: "idle" },
  outside_hours: { label: "非工作时间", tone: "idle" },
  blocked_hours: { label: "屏蔽时段", tone: "idle" },
  monitor_only: { label: "夜间仅记录", tone: "info" },
  stream_down: { label: "视频流断开", tone: "bad" },
  no_baseline: { label: "缺少校准基线", tone: "warn" },
  audit_required: { label: "等待机位体检", tone: "warn" },
  calibrating: { label: "正在校准", tone: "info" },
  auditing: { label: "正在体检", tone: "info" },
  starting: { label: "启动中", tone: "idle" },
  unconfigured: { label: "未配置视频流", tone: "idle" },
  model_error: { label: "姿态模型不可用", tone: "bad" },
};

const CHART_MINUTES = 15;
const REFRESH_MS = 2000;

// Human wording for the schedule reasons the API reports.  Kept next to
// STATE_INFO so a new reason in posture/schedule.py shows up as "非允许时段"
// rather than an empty string.
const REASON_LABELS = {
  night: "夜间 0-8 点",
  school_hours: "上学时段",
  late_night: "夜间 22 点后",
  weekend: "周末",
  holiday: "法定节假日",
  school_break: "寒暑假",
  allowed: "允许检测",
};

const MODE_LABELS = {
  allow: "检测+播报",
  monitor_only: "仅记录",
  block: "不检测",
};

function reasonLabel(service) {
  return REASON_LABELS[service.schedule_reason] || "非允许时段";
}

function scheduleTimeText(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  const month = date.getMonth() + 1;
  const day = String(date.getDate()).padStart(2, "0");
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  return `${month}-${day} ${hour}:${minute}`;
}

const state = {
  data: null,
  series: [],
  lastT: null,
  timer: null,
  inFlight: false,
  failures: 0,
  hover: null,
  clockOffset: 0,
  history: null,
  historyHours: null,
  historyHover: null,
  historyLoading: false,
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function parseTime(value) {
  if (!value) return NaN;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : NaN;
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function formatClock(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

function formatShortClock(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

function formatPercent(value, digits = 0) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const pct = Number(value) * 100;
  return pct.toFixed(pct < 10 && digits === 0 ? 1 : digits);
}

function formatNumber(value, digits) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toFixed(digits);
}

function formatSeconds(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const seconds = Number(value);
  if (seconds >= 60) {
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds - minutes * 60);
    return `${minutes} 分 ${pad2(rest)} 秒`;
  }
  return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
}

function serverDate() {
  return new Date(Date.now() + state.clockOffset);
}

function clockText() {
  const now = state.data ? serverDate() : new Date();
  const weekdays = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  return `${now.getFullYear()} 年 ${now.getMonth() + 1} 月 ${now.getDate()} 日 ${weekdays[now.getDay()]} ${formatClock(now)}`;
}

/* ------------------------------------------------------------------ */
/* Decision explanation                                                */
/* ------------------------------------------------------------------ */
function currentIssueLabel(scoring) {
  if (!scoring || !scoring.issue) return "无";
  return ISSUE_LABELS[scoring.issue] || scoring.issue;
}

function auditSummary(service) {
  const audit = service.camera_audit;
  if (!audit) return "";
  if (audit.passed) return "";
  const checks = audit.checks || {};
  const parts = [];
  if (checks.view_is_side === false) parts.push("视角不在正侧方");
  if (checks.detection_rate === false) {
    parts.push(`检出率 ${formatPercent(audit.detection_rate, 1)}%`);
  }
  if (checks.shoulder_width === false) {
    parts.push(`肩宽 ${formatNumber(audit.shoulder_width_pct, 1)}%`);
  }
  if (checks.hip_visible === false) {
    parts.push(`髋部可见率 ${formatPercent(audit.hip_visible_rate, 0)}%`);
  }
  if (checks.head_fwd_stable === false) {
    parts.push(`夹角抖动 ${formatNumber(audit.head_fwd_jitter_deg, 2)}°`);
  }
  if (checks.head_fwd_signal === false) {
    parts.push(`5 秒变化仅 ${formatNumber(audit.head_fwd_range_5s_deg, 1)}°`);
  }
  if (checks.duration === false) parts.push("体检时长不足");
  if (!parts.length) {
    const reasons = Array.isArray(audit.reasons) ? audit.reasons : [];
    if (reasons.length) parts.push(reasons[0]);
  }
  return parts.length ? `${parts.join("、")}，未达到体检要求` : "未达到体检要求";
}

function decisionView(data) {
  const service = data.service || {};
  const scoring = data.scoring;
  const window = scoring ? scoring.window : null;
  const stateKey = service.state || "starting";
  const info = STATE_INFO[stateKey] || { label: stateKey, tone: "idle" };
  const view = {
    key: stateKey,
    label: info.label,
    tone: info.tone,
    reason: "",
  };

  if (stateKey === "unconfigured") {
    view.reason = "没有配置 POSTURE_RTSP_URL，服务只能提供界面，不能采集画面。";
  } else if (stateKey === "model_error") {
    view.reason = service.error || "姿态模型没有加载，无法计算三个指标。";
  } else if (stateKey === "calibrating") {
    view.reason = "正在采集 2 秒的校准样本，完成后会更新基线；期间暂停判定。";
  } else if (stateKey === "auditing") {
    view.reason = "正在做约 20 秒的机位体检，期间暂停判定。";
  } else if (stateKey === "stream_down") {
    view.reason = "视频流断开，服务正在退避重连；基线不会丢失。";
  } else if (stateKey === "blocked_hours" || service.schedule_mode === "block") {
    view.key = "blocked_hours";
    view.label = STATE_INFO.blocked_hours.label;
    view.tone = STATE_INFO.blocked_hours.tone;
    view.reason = `当前处于屏蔽时段（${reasonLabel(service)}），不检测、不播报；${scheduleTimeText(service.next_allow_at)}起恢复。`;
  } else if (service.schedule_mode === "monitor_only") {
    view.key = "monitor_only";
    view.label = STATE_INFO.monitor_only.label;
    view.tone = STATE_INFO.monitor_only.tone;
    view.reason = "22 点后仍在检测并记录，但不会出声；次日恢复时段内才会播报。";
  } else if (stateKey === "outside_hours") {
    view.key = "outside_hours";
    view.label = STATE_INFO.outside_hours.label;
    view.tone = STATE_INFO.outside_hours.tone;
    view.reason = `当前处于屏蔽时段（${reasonLabel(service)}），不检测、不播报；${scheduleTimeText(service.next_allow_at)}起恢复。`;
  } else if (stateKey === "no_baseline") {
    view.reason = "还没有校准基线；先让孩子以正常写字姿势校准一次。";
  } else if (stateKey === "audit_required") {
    if (service.camera_audit_passed === false) {
      view.label = "机位体检未通过";
    }
    const summary = auditSummary(service);
    view.reason = summary
      ? `机位体检未通过，播报保持关闭：${summary}。`
      : "机位体检还未通过，播报保持关闭。";
  } else if (!scoring) {
    view.reason = "暂时没有可用的判定数据。";
  } else {
    const issue = currentIssueLabel(scoring);
    const occupancy = window ? formatPercent(window.occupancy, 0) : "—";
    const threshold = window ? formatPercent(window.occupancy_threshold, 0) : "55";
    switch (stateKey) {
      case "ok":
        view.reason = "最近 3 秒的抬头高度和耳肩髋夹角都在阈值内，不需要提醒。";
        break;
      case "out_of_line":
        view.reason = `当前判为「${issue}」；20 秒窗口内越线占比 ${occupancy}%，还没到 ${threshold}%。`;
        break;
      case "warming":
        view.reason = `当前判为「${issue}」；窗口才 ${formatSeconds(window && window.span_seconds)}，至少要接近 ${formatNumber(window && window.seconds, 0)} 秒才判断。`;
        break;
      case "cooldown": {
        const remaining = scoring.cooldown_remaining && scoring.cooldown_remaining[scoring.issue];
        view.reason = `当前判为「${issue}」；同类话术冷却中，还剩 ${formatSeconds(remaining)}。`;
        break;
      }
      case "hourly_cap":
        view.reason = `当前判为「${issue}」；本小时已播报 ${scoring.hourly_count} / ${scoring.hourly_cap} 次，达到上限，静默到下一个整点。`;
        break;
      case "reminded":
        view.reason = `刚刚因「${issue}」触发了一次播报。`;
        break;
      case "reminder_blocked":
        view.reason = "判定已经满足，但机位体检未通过，播报被禁止。";
        break;
      case "no_person":
        view.reason = "最近没有检出姿态，20 秒窗口不会累计；等孩子回到画面后重新开始。";
        break;
      default:
        view.reason = "服务正在等待第一个可用样本。";
    }
  }

  // An audit failure is the gating reason even when the engine itself says
  // "reminder_blocked"; keep the explanation consistent with the banner.
  if (
    (stateKey === "audit_required" || stateKey === "reminder_blocked") &&
    service.require_audit &&
    service.camera_audit_passed === false &&
    service.schedule_mode !== "block"
  ) {
    const summary = auditSummary(service);
    if (summary) view.reason = `机位体检未通过，播报被禁止：${summary}。`;
  }

  return view;
}

function noticeFor(data) {
  const service = data.service || {};
  if (service.error && service.state !== "unconfigured") {
    return { tone: "bad", text: `服务报错：${service.error}` };
  }
  if (service.state === "stream_down") {
    return { tone: "bad", text: "视频流断开，服务正在自动重连。" };
  }
  if (service.require_audit && service.camera_audit_passed === false) {
    return {
      tone: "warn",
      text: `机位体检未通过，播报被禁止：${auditSummary(service)}。`,
    };
  }
  if (service.schedule_mode === "block") {
    return null;
  }
  if (service.require_audit && service.camera_audit_passed === null) {
    return {
      tone: "info",
      text: "还没有完成机位体检。孩子坐下后会尝试自动体检，通过之前不会播报。",
    };
  }
  if (!service.speaking_enabled) {
    return {
      tone: "info",
      text: "当前是空跑模式：会照常判定和记录，但不会让音箱发声。",
    };
  }
  return null;
}

/* ------------------------------------------------------------------ */
/* Renderers                                                           */
/* ------------------------------------------------------------------ */
function renderClock() {
  $("clock").textContent = clockText();
}

function setBadge(elementId, text, tone) {
  const element = $(elementId);
  element.textContent = text;
  element.className = `badge badge-${tone}`;
}

function renderService(data) {
  const service = data.service || {};
  let tone = "ok";
  let text = "运行正常";
  if (service.state === "unconfigured") {
    tone = "idle";
    text = "未配置视频流";
  } else if (service.status !== "ok") {
    tone = "warn";
    text = "部分受限";
  }
  if (["stream_down", "model_error"].includes(service.state)) {
    tone = "bad";
    text = "服务异常";
  } else if (service.schedule_mode === "block") {
    tone = "idle";
    text = "屏蔽时段";
  } else if (service.schedule_mode === "monitor_only") {
    tone = "idle";
    text = "夜间仅记录";
  }
  setBadge("service-badge", text, tone);
  $("updated-at").textContent = `更新于 ${formatClock(serverDate())}`;

  const notice = noticeFor(data);
  const box = $("notice");
  const textBox = $("notice-text");
  if (notice) {
    box.hidden = false;
    box.className = `notice is-${notice.tone}`;
    textBox.textContent = notice.text;
  } else {
    box.hidden = true;
  }

  const facts = [];
  const streamTone = service.stream_connected ? "is-ok" : service.state === "unconfigured" ? "is-muted" : "is-bad";
  facts.push({
    label: "视频流",
    value: service.stream_connected ? `已连接 · ${formatNumber(service.fps, 1)} fps` : "断开",
    tone: streamTone,
  });
  facts.push({
    label: "孩子",
    value: service.person ? "在画面中" : "未检测到",
    tone: service.person ? "is-ok" : "is-muted",
  });
  let auditValue = "未完成";
  let auditTone = "is-muted";
  if (service.camera_audit_passed === true) {
    auditValue = "通过";
    auditTone = "is-ok";
  } else if (service.camera_audit_passed === false) {
    auditValue = "未通过";
    auditTone = "is-bad";
  } else if (service.require_audit === false) {
    auditValue = "不要求";
  }
  facts.push({ label: "机位体检", value: auditValue, tone: auditTone });
  facts.push({
    label: "播报方式",
    value: service.speaking_enabled ? "已开启" : "空跑（只记录）",
    tone: service.speaking_enabled ? "is-ok" : "is-warn",
  });
  facts.push({
    label: "时段规则",
    value: `${reasonLabel(service)} · ${MODE_LABELS[service.schedule_mode] || "—"}`,
    tone: service.schedule_mode === "allow" ? "is-ok" : "is-muted",
  });
  facts.push({
    label: "校准基线",
    value: service.baseline_present
      ? `抬 ${formatNumber(service.baseline && service.baseline.head_up, 3)} · 夹 ${formatNumber(service.baseline && service.baseline.head_fwd, 1)}°`
      : "未校准",
    tone: service.baseline_present ? "is-ok" : "is-warn",
  });
  // The holiday library only covers 2004-2027; past that the service keeps the
  // daytime block on (fail-closed), so surface it instead of silently not
  // detecting on weekday mornings.
  if (service.calendar_ok === false) {
    facts.push({
      label: "节假日日历",
      value: "数据已过期，白天按上学日处理",
      tone: "is-bad",
    });
  }
  $("system-facts").innerHTML = facts
    .map(
      (fact) =>
        `<li><span class="fact-label">${escapeHtml(fact.label)}</span><span class="fact-value ${fact.tone}">${escapeHtml(fact.value)}</span></li>`
    )
    .join("");
}

function renderDecision(data) {
  const view = decisionView(data);
  const scoring = data.scoring;
  const service = data.service || {};
  const card = $("decision-card");
  card.className = `card decision-card${view.tone === "ok" || view.tone === "warn" || view.tone === "bad" ? ` is-${view.tone}` : ""}`;
  $("decision-title").textContent = view.label;
  $("decision-reason").textContent = view.reason;
  $("decision-tag").textContent = view.key;

  const meta = [];
  if (scoring) {
    const issueLabel = scoring.issue
      ? ISSUE_LABELS[scoring.issue] || scoring.issue
      : scoring.smoothed
        ? "正常"
        : "—";
    meta.push(`当前类别 <strong>${escapeHtml(issueLabel)}</strong>`);
  }
  if (service.stream_connected) {
    meta.push(`视频 <strong>${formatNumber(service.fps, 1)} fps</strong>`);
  }
  meta.push(`<strong>${service.person ? "有人" : "无人"}</strong>`);
  if (service.camera_audit_passed === false) {
    meta.push("播报 <strong>被体检拦截</strong>");
  } else if (!service.speaking_enabled) {
    meta.push("播报 <strong>空跑</strong>");
  }
  $("decision-meta").innerHTML = meta.map((item) => `<span>${item}</span>`).join("");
}

function renderMetrics(data) {
  const scoring = data.scoring;
  const baseline = scoring && scoring.baseline;
  const smoothed = scoring && scoring.smoothed;
  const thresholds = (scoring && scoring.thresholds) || {};
  const policy = (scoring && scoring.policy) || {};
  const hasData = Boolean(baseline);
  $("metrics-empty").hidden = hasData && Boolean(smoothed);
  $("baseline-pill").textContent = hasData
    ? `基线 · 抬 ${formatNumber(baseline.head_up, 3)} · 夹 ${formatNumber(baseline.head_fwd, 1)}°`
    : "基线 —";

  if (!hasData) {
    $("metrics-list").innerHTML = "";
    return;
  }

  const defs = [
    {
      key: "head_up",
      name: "抬头高度",
      code: "head_up",
      unit: "",
      digits: 3,
      direction: "min",
      threshold: thresholds.head_up,
      domain: [0, Math.max(0.8, baseline.head_up * 2.2)],
      foot: `阈值 ${formatNumber(thresholds.head_up, 3)}（基线的 ${formatNumber((policy.head_up_ratio ?? 0.5) * 100, 0)}%）`,
    },
    {
      key: "head_fwd",
      name: "耳肩髋夹角",
      code: "head_fwd",
      unit: "°",
      digits: 1,
      direction: "min",
      threshold: thresholds.head_fwd,
      domain: [100, 190],
      foot: `阈值 ${formatNumber(thresholds.head_fwd, 1)}°（基线 − ${formatNumber(policy.head_fwd_drop, 0)}°）`,
    },
    {
      key: "s_norm",
      name: "肩宽 / 画面宽",
      code: "s_norm",
      unit: "",
      digits: 4,
      direction: null,
      threshold: thresholds.too_close_s_norm,
      domain: [0, Math.max(0.12, baseline.s_norm * 2)],
      foot: "仅用于区分「前塌」和「侧歪」",
    },
  ];

  const html = defs
    .map((def) => {
      const value = smoothed ? Number(smoothed[def.key]) : null;
      const idle = value == null || Number.isNaN(value);
      const out =
        !idle && def.direction === "min" && def.threshold != null && value < Number(def.threshold);
      const base = Number(baseline[def.key]);
      const threshold = Number(def.threshold);
      const [lo, hi] = def.domain;
      const pos = (number) => {
        if (!Number.isFinite(number) || hi <= lo) return 50;
        return Math.min(100, Math.max(0, ((number - lo) / (hi - lo)) * 100));
      };
      const basePercent = base ? pos(base) : null;
      const thresholdPercent = pos(threshold);
      const safeZone =
        def.direction === "min"
          ? `<div class="metric-zone is-risk" style="left:0;width:${thresholdPercent}%"></div>
             <div class="metric-zone is-safe" style="left:${thresholdPercent}%;right:0"></div>`
          : "";
      let stateText = "参考";
      if (def.direction === "min") stateText = idle ? "等待数据" : out ? "越线" : "正常";
      else stateText = idle ? "等待数据" : "参考";

      let compare = "—";
      if (!idle && base) {
        if (def.key === "head_fwd") {
          const delta = value - base;
          compare = `${delta >= 0 ? "高于" : "低于"}基线 ${Math.abs(delta).toFixed(1)}°`;
        } else {
          compare = `当前 / 基线 ${Math.round((value / base) * 100)}%`;
        }
      }
      return `
        <div class="metric ${out ? "is-out" : ""} ${idle ? "is-idle" : ""}">
          <div class="metric-head">
            <span class="metric-name">${def.name}<small>${def.code}</small></span>
            <span class="metric-state">${stateText}</span>
          </div>
          <div class="metric-value">${idle ? "—" : value.toFixed(def.digits)}<small>${def.unit}${idle ? "" : ""}</small></div>
          <div class="metric-track">
            ${safeZone}
            <div class="metric-marker is-baseline" style="left:${basePercent}%"></div>
            <div class="metric-marker is-threshold" style="left:${thresholdPercent}%"></div>
            ${idle ? "" : `<div class="metric-dot" style="left:${pos(value)}%"></div>`}
          </div>
          <div class="metric-foot">
            <span>${def.foot}</span>
            <span class="mono">${idle ? "—" : compare}</span>
          </div>
        </div>`;
    })
    .join("");
  $("metrics-list").innerHTML = html;
}

function renderWindow(data) {
  const scoring = data.scoring;
  const window = scoring && scoring.window;
  const occupancy = window ? Number(window.occupancy) : null;
  const threshold = window ? Number(window.occupancy_threshold) : 0.55;
  const span = window ? Number(window.span_seconds) : null;
  const seconds = window ? Number(window.seconds) : 20;
  const minSpan = window ? Number(window.min_span_seconds) : seconds - 1;
  const issue = scoring && scoring.issue;

  $("occupancy-value").textContent = occupancy == null ? "—" : formatPercent(occupancy, 0);
  $("occupancy-threshold").textContent = formatPercent(threshold, 0);
  $("occupancy-mark").style.left = `${threshold * 100}%`;
  const fill = $("occupancy-fill");
  fill.style.width = `${Math.min(100, Math.max(0, (occupancy || 0) * 100))}%`;
  fill.className = "gauge-fill";
  if (occupancy != null && occupancy >= threshold) {
    fill.classList.add(issue ? "is-danger" : "is-over");
  }

  let windowPill = "—";
  if (window) {
    windowPill = span >= minSpan ? "窗口已满" : `采集中 ${Math.round(span)}s / ${Math.round(seconds)}s`;
  }
  $("window-pill").textContent = windowPill;

  const facts = [];
  facts.push({
    label: "窗口时长",
    value: window ? `${span.toFixed(1)} / ${seconds.toFixed(0)} 秒` : "—",
  });
  facts.push({ label: "窗口样本", value: window ? `${window.samples} 个` : "—" });
  const hasWindowData = Boolean(window && window.samples > 0);
  facts.push({
    label: "当前样本",
    value: issue
      ? `越线（${ISSUE_LABELS[issue] || issue}）`
      : hasWindowData
        ? "正常"
        : "—",
    tone: issue ? "is-bad" : hasWindowData ? "is-ok" : "is-muted",
  });
  let verdict = "—";
  let verdictTone = "is-muted";
  if (window) {
    if (!hasWindowData) {
      verdict = "等待样本";
      verdictTone = "is-muted";
    } else if (span < minSpan) {
      verdict = "窗口采集中";
      verdictTone = "is-warn";
    } else if (occupancy >= threshold && issue) {
      verdict = "达到提醒条件";
      verdictTone = "is-bad";
    } else if (occupancy >= threshold) {
      verdict = "占比达标，但当前未越线";
      verdictTone = "is-warn";
    } else {
      verdict = "未达到提醒条件";
      verdictTone = "is-ok";
    }
  }
  facts.push({ label: "窗口判定", value: verdict, tone: verdictTone });
  $("window-facts").innerHTML = facts
    .map(
      (fact) =>
        `<li><span class="fact-label">${escapeHtml(fact.label)}</span><span class="fact-value ${fact.tone || ""}">${escapeHtml(fact.value)}</span></li>`
    )
    .join("");
}

function renderActions(data) {
  const service = data.service || {};
  const scoring = data.scoring;
  const reminders = Array.isArray(data.reminders) ? data.reminders : [];
  $("reminders-today").textContent = String(service.reminders_today ?? 0);
  $("speak-pill").textContent = service.speaking_enabled ? "已开启" : "空跑";

  const facts = [];
  if (scoring) {
    facts.push({
      label: "本小时播报",
      value: `${scoring.hourly_count} / ${scoring.hourly_cap} 次`,
    });
    const remaining = scoring.cooldown_remaining || {};
    const cooling = Object.entries(remaining)
      .filter(([, value]) => Number(value) > 0)
      .map(([issue, value]) => `${ISSUE_LABELS[issue] || issue} ${formatSeconds(value)}`);
    facts.push({
      label: "同类冷却",
      value: cooling.length ? cooling.join(" · ") : "无",
      tone: cooling.length ? "is-warn" : "is-muted",
    });
  }
  facts.push({
    label: "播报出口",
    value: service.speaking_enabled ? "DLNA 音箱" : "空跑（只记录）",
    tone: service.speaking_enabled ? "is-ok" : "is-warn",
  });
  $("action-facts").innerHTML = facts
    .map(
      (fact) =>
        `<li><span class="fact-label">${escapeHtml(fact.label)}</span><span class="fact-value ${fact.tone || ""}">${escapeHtml(fact.value)}</span></li>`
    )
    .join("");

  const last = service.last_reminder;
  const latest = reminders[0];
  let lastText = "今天还没有播报。";
  if (last) {
    lastText = `${formatShortClock(last.at)} · ${ISSUE_LABELS[last.issue] || last.issue}「${last.text}」`;
  } else if (latest) {
    lastText = `${formatShortClock(latest.at)} · ${ISSUE_LABELS[latest.issue] || latest.issue}`;
  }
  $("last-reminder").textContent = lastText;
}

function renderAlerts() {
  const history = state.history;
  const alerts = (history && history.reminders) || [];
  const enabled = Boolean(
    (history && history.alert_images_enabled) ||
      (state.data && state.data.service && state.data.service.alert_images_enabled)
  );
  const scoring = (history && history.scoring) || (state.data && state.data.scoring) || {};
  const thresholds = scoring.thresholds || {};
  const baseline = scoring.baseline || {};
  const list = $("alert-list");
  $("alert-count").textContent = alerts.length ? `${alerts.length} 次` : "—";
  $("alert-empty").hidden = alerts.length > 0;
  $("alert-off").hidden = !alerts.length || enabled;
  if (!alerts.length) {
    list.innerHTML = "";
    return;
  }
  list.innerHTML = alerts
    .map((reminder) => {
      const label = ISSUE_LABELS[reminder.issue] || reminder.issue;
      const imageName = reminder.image;
      const image =
        enabled && imageName
          ? `<a class="alert-thumb" href="/api/alert-image/${encodeURIComponent(imageName)}" target="_blank" rel="noreferrer">
               <img loading="lazy" src="/api/alert-image/${encodeURIComponent(imageName)}" alt="${escapeHtml(label)}报警图片">
             </a>`
          : `<span class="alert-thumb alert-thumb-empty">${enabled ? "无图片" : "未开启图片"}</span>`;
      const headUp = reminder.head_up == null ? null : Number(reminder.head_up);
      const headFwd = reminder.head_fwd == null ? null : Number(reminder.head_fwd);
      const shoulder = reminder.s_norm == null ? null : Number(reminder.s_norm);
      const metrics = [];
      if (headUp != null) {
        metrics.push(
          `<span>抬头 <b>${headUp.toFixed(3)}</b>${
            thresholds.head_up != null ? ` <small>阈值 ${Number(thresholds.head_up).toFixed(3)}</small>` : ""
          }</span>`
        );
      }
      if (headFwd != null) {
        metrics.push(
          `<span>夹角 <b>${headFwd.toFixed(1)}°</b>${
            thresholds.head_fwd != null ? ` <small>阈值 ${Number(thresholds.head_fwd).toFixed(1)}°</small>` : ""
          }</span>`
        );
      }
      if (shoulder != null) {
        metrics.push(
          `<span>肩宽 <b>${shoulder.toFixed(4)}</b>${
            baseline.s_norm != null ? ` <small>基线 ${Number(baseline.s_norm).toFixed(4)}</small>` : ""
          }</span>`
        );
      }
      const verdict = reminder.verdict || "";
      const note = reminder.verdict_note || "";
      const itemClass = verdict
        ? `alert-item verdict-${verdict === "true_positive" ? "true" : "false"}`
        : "alert-item";
      const trueActive = verdict === "true_positive" ? " is-active" : "";
      const falseActive = verdict === "false_positive" ? " is-active" : "";
      const verdictLabel = verdict === "true_positive"
        ? "已标：正报"
        : verdict === "false_positive"
          ? "已标：误报"
          : verdict === "invalid"
            ? "已作废（不参与调参）"
            : "未判定";
      return `
        <div class="${itemClass}" data-id="${escapeHtml(String(reminder.id))}">
          ${image}
          <div class="alert-body">
            <div class="alert-line">
              <span class="alert-time">${escapeHtml(formatClock(reminder.at))}</span>
              <span class="issue-chip">${escapeHtml(label)}</span>
              <span class="alert-note">20 秒窗口触发</span>
              <span class="verdict-badge">${verdictLabel}</span>
            </div>
            <div class="alert-metrics">${metrics.join("")}</div>
            <div class="verdict-controls">
              <button type="button" class="verdict-btn verdict-btn-true${trueActive}" data-verdict="true_positive">正报</button>
              <button type="button" class="verdict-btn verdict-btn-false${falseActive}" data-verdict="false_positive">误报</button>
              <button type="button" class="verdict-btn verdict-btn-invalid${verdict === "invalid" ? " is-active" : ""}" data-verdict="invalid" title="无人/无效事件，不参与调参">作废</button>
              <input type="text" class="verdict-note" maxlength="120" placeholder="备注（可选）" value="${escapeHtml(note)}">
              ${verdict ? '<button type="button" class="verdict-btn verdict-btn-clear" data-verdict="">清除</button>' : ""}
            </div>
          </div>
        </div>`;
    })
    .join("");
}

/* ------------------------------------------------------------------ */
/* Human verdicts + threshold tuning                                   */
/* ------------------------------------------------------------------ */
function toneClass(confidence) {
  if (confidence === "high") return "ok";
  if (confidence === "medium") return "warn";
  return "idle";
}

function renderTuning(data) {
  const confidence = data.confidence || "low";
  setBadge("tuning-confidence", `置信度 ${confidence}`, toneClass(confidence));
  const body = $("tuning-body");
  const actions = $("tuning-actions");
  if (data.error) {
    body.innerHTML = `<p class="muted">${escapeHtml(data.note || "暂不可用")}</p>`;
    actions.hidden = true;
    return;
  }
  const changes = data.changes || {};
  const changeKeys = Object.keys(changes);
  const policy = data.current_policy || {};
  const labels = {
    head_up_ratio: "抬头比例 head_up_ratio",
    head_fwd_drop: "夹角降幅 head_fwd_drop",
    too_close_ratio: "过近比例 too_close_ratio",
    occupancy_threshold: "占空阈值 occupancy_threshold",
  };
  const rows = changeKeys.map((key) => {
    const from = policy[key];
    const to = changes[key];
    return `<li><b>${escapeHtml(labels[key] || key)}</b>：${escapeHtml(String(from))} → <strong>${escapeHtml(String(to))}</strong></li>`;
  }).join("");
  const counts = data.per_issue || {};
  const missedBy = data.missed_by_issue || {};
  const countRows = Object.keys(counts).map((issue) => {
    const c = counts[issue] || {};
    const miss = Number(missedBy[issue] || 0);
    return `<span class="tuning-count">${escapeHtml(ISSUE_LABELS[issue] || issue)}：正报 ${Number(c.true_positive || 0)} / 误报 ${Number(c.false_positive || 0)} / 漏报 ${miss}</span>`;
  }).join("");
  const overrideNote = data.override_active
    ? '<p class="tuning-overriden">当前已有人工覆盖阈值生效中。</p>'
    : "";
  const conflictRows = (data.conflicts || []).map(
    (c) => `<p class="tuning-conflict">${escapeHtml(c)}</p>`
  ).join("");
  body.innerHTML = `
    <p>${escapeHtml(data.note || "")}</p>
    <div class="tuning-counts">${countRows || '<span class="muted">还没有任何人工判定</span>'}</div>
    ${rows ? `<ul class="tuning-changes">${rows}</ul>` : '<p class="muted">当前没有需要调整的建议。</p>'}
    ${(data.reasons || []).map((r) => `<p class="tuning-reason">· ${escapeHtml(r)}</p>`).join("")}
    ${conflictRows}
    ${overrideNote}
  `;
  actions.hidden = changeKeys.length === 0;
  $("tuning-apply").dataset.suggestion = JSON.stringify(changes);
}

async function loadTuning() {
  try {
    const response = await fetch("/api/tuning", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderTuning(await response.json());
  } catch (error) {
    console.error("tuning load failed", error);
    $("tuning-body").innerHTML = '<p class="muted">建议加载失败，请点「刷新」重试。</p>';
  }
}

async function submitVerdict(button) {
  const item = button.closest(".alert-item");
  if (!item) return;
  const id = item.dataset.id;
  const verdict = button.dataset.verdict || null;
  const noteEl = item.querySelector(".verdict-note");
  const note = noteEl ? noteEl.value : "";
  button.disabled = true;
  try {
    const body = { id: Number(id), verdict };
    if (note) body.note = note;
    const response = await fetch("/api/alert-verdict", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    await loadHistory();
    loadTuning();
  } catch (error) {
    console.error("verdict submit failed", error);
    window.alert("判定保存失败：" + (error.message || "未知错误"));
    button.disabled = false;
  }
}

async function applyTuning(clear) {
  const btn = $("tuning-apply");
  const status = $("tuning-status");
  status.hidden = false;
  status.textContent = "正在写入…";
  try {
    let values = {};
    if (!clear) {
      try {
        values = JSON.parse(btn.dataset.suggestion || "{}");
      } catch (_) {
        values = {};
      }
    }
    const response = await fetch("/api/apply-thresholds", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    status.textContent = (clear ? "已清除覆盖。" : "已保存新阈值。") + " 需重启 posture-pr2 服务后生效；请让家长确认后重启。";
    loadTuning();
  } catch (error) {
    status.textContent = "写入失败：" + (error.message || "未知错误");
  }
}

function bindVerdicts() {
  const list = $("alert-list");
  list.addEventListener("click", (event) => {
    const btn = event.target.closest(".verdict-btn");
    if (btn) submitVerdict(btn);
  });
  $("tuning-apply").addEventListener("click", () => applyTuning(false));
  $("tuning-reset").addEventListener("click", () => {
    if (window.confirm("清除人工阈值覆盖、恢复默认？")) applyTuning(true);
  });
}

/* ------------------------------------------------------------------ */
/* Missed-event marking                                                */
/* ------------------------------------------------------------------ */
function toLocalInput(ms) {
  const d = new Date(ms - (state.clockOffset || 0));
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fromLocalInput(value) {
  const d = new Date(value);
  return new Date(d.getTime() + (state.clockOffset || 0));
}

function renderMissed(events) {
  const list = $("missed-list");
  $("missed-count").textContent = events.length ? `${events.length} 条` : "—";
  $("missed-empty").hidden = events.length > 0;
  if (!events.length) {
    list.innerHTML = "";
    return;
  }
  list.innerHTML = events
    .map((ev) => {
      const label = ISSUE_LABELS[ev.issue] || ev.issue;
      const metrics = [];
      if (Number.isFinite(ev.head_up)) metrics.push(`抬头 ${Number(ev.head_up).toFixed(3)}`);
      if (Number.isFinite(ev.head_fwd)) metrics.push(`夹角 ${Number(ev.head_fwd).toFixed(1)}°`);
      const snapshot = metrics.length ? `<span class="missed-snapshot">${escapeHtml(metrics.join(" · "))}</span>` : '<span class="missed-snapshot muted">无同期数据</span>';
      const options = Object.keys(ISSUE_LABELS)
        .map((key) => `<option value="${key}"${key === ev.issue ? " selected" : ""}>${escapeHtml(ISSUE_LABELS[key])}</option>`)
        .join("");
      return `
        <div class="missed-item" data-id="${escapeHtml(String(ev.id))}">
          <div class="missed-line">
            <span class="alert-time">${escapeHtml(formatClock(ev.at))}</span>
            <span class="missed-fixed-chip">${escapeHtml(label)}</span>
            ${snapshot}
            <button type="button" class="missed-del" title="删除">删除</button>
          </div>
          <div class="missed-edit">
            <select class="missed-edit-issue">${options}</select>
            <input type="text" class="missed-edit-note" maxlength="120" placeholder="备注（可选）" value="${escapeHtml(ev.note || "")}">
            <button type="button" class="missed-save">保存</button>
          </div>
        </div>`;
    })
    .join("");
}

/* ------------------------------------------------------------------ */
/* Look-back timeline: pick a 5-minute bucket and mark a missed event   */
/* ------------------------------------------------------------------ */
/* ------------------------------------------------------------------ */
/* Look-back timeline: zoom, pan, pick a moment, mark a missed event    */
/* ------------------------------------------------------------------ */
const TIMELINE = {
  start: 0,          // view window start (epoch ms)
  end: 0,            // view window end (epoch ms)
  buckets: [],
  bucketSeconds: 300,
  picked: null,
  hoverIndex: null,
  dragging: false,
  dragStartX: 0,
  dragStartWindow: null,
  movedDuringDrag: false,
  loading: false,
  initialised: false,
};

const TIMELINE_MIN_SPAN_MS = 60 * 1000;      // 1 minute — 1 s buckets are usable
const TIMELINE_MAX_SPAN_MS = 25 * 60 * 60 * 1000;

function timelineBucketFor(spanMs) {
  // Target ~120 bars across the view.
  const target = spanMs / 1000 / 120;
  if (target <= 1) return 1;
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  for (const step of steps) {
    if (target <= step) return step;
  }
  return 3600;
}

function timelineGeometry() {
  const canvas = $("timeline-chart");
  const wrap = canvas && canvas.parentElement;
  if (!canvas || !wrap) return null;
  const rect = wrap.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const pad = { left: 8, right: 8, top: 8, bottom: 22 };
  return {
    canvas,
    width: rect.width,
    height: rect.height,
    pad,
    plotWidth: rect.width - pad.left - pad.right,
    plotHeight: rect.height - pad.top - pad.bottom,
  };
}

function timelineInitialiseView() {
  if (TIMELINE.initialised) return;
  const now = Date.now() + (state.clockOffset || 0);
  TIMELINE.end = now;
  TIMELINE.start = now - 24 * 60 * 60 * 1000;
  TIMELINE.initialised = true;
}

async function loadTimeline() {
  timelineInitialiseView();
  const span = TIMELINE.end - TIMELINE.start;
  const bucket = timelineBucketFor(span);
  TIMELINE.bucketSeconds = bucket;
  TIMELINE.loading = true;
  try {
    const params = new URLSearchParams({
      start: new Date(TIMELINE.start).toISOString(),
      end: new Date(TIMELINE.end).toISOString(),
      bucket_seconds: String(bucket),
    });
    const response = await fetch(`/api/timeline?${params.toString()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    TIMELINE.buckets = data.buckets || [];
  } catch (error) {
    console.error("timeline load failed", error);
    TIMELINE.buckets = [];
  } finally {
    TIMELINE.loading = false;
    drawTimeline();
    updateTimelineLabel();
  }
}

function updateTimelineLabel() {
  const span = TIMELINE.end - TIMELINE.start;
  const minutes = span / 60000;
  const spanText = minutes >= 60
    ? `${(minutes / 60).toFixed(minutes % 60 === 0 ? 0 : 1)} 小时`
    : minutes >= 1
      ? `${Math.round(minutes)} 分钟`
      : `${Math.round(span / 1000)} 秒`;
  const bucketText = TIMELINE.bucketSeconds >= 60
    ? `${TIMELINE.bucketSeconds / 60} 分钟/段`
    : `${TIMELINE.bucketSeconds} 秒/段`;
  $("timeline-pill").textContent = `${formatShortClock(new Date(TIMELINE.start).toISOString())}–${formatShortClock(new Date(TIMELINE.end).toISOString())} · ${spanText} · ${bucketText}`;
}

function timelineXForTime(ms, geo) {
  const span = TIMELINE.end - TIMELINE.start;
  if (span <= 0) return geo.pad.left;
  return geo.pad.left + ((ms - TIMELINE.start) / span) * geo.plotWidth;
}

function timelineTimeForX(x, geo) {
  const span = TIMELINE.end - TIMELINE.start;
  return TIMELINE.start + ((x - geo.pad.left) / geo.plotWidth) * span;
}

function drawTimeline() {
  const geo = timelineGeometry();
  if (!geo) return;
  const { canvas, width, height, pad, plotWidth, plotHeight } = geo;
  $("timeline-empty").hidden = TIMELINE.buckets.length > 0 || TIMELINE.loading;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  // Baseline
  ctx.strokeStyle = "#e4e8ee";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(pad.left, pad.top + plotHeight + 0.5);
  ctx.lineTo(width - pad.right, pad.top + plotHeight + 0.5);
  ctx.stroke();

  if (!TIMELINE.buckets.length) {
    if (!TIMELINE.loading) {
      ctx.fillStyle = "#75808e";
      ctx.font = "12px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("该时段没有记录", width / 2, pad.top + plotHeight / 2);
    }
    return;
  }

  const span = TIMELINE.end - TIMELINE.start;
  const pxPerMs = plotWidth / Math.max(1, span);
  const bucketWidthPx = Math.max(1, (TIMELINE.bucketSeconds * 1000) * pxPerMs - 0.5);

  TIMELINE.buckets.forEach((bucket, index) => {
    const t = parseTime(bucket.t);
    if (!Number.isFinite(t)) return;
    const x = timelineXForTime(t, geo);
    if (x + bucketWidthPx < pad.left || x > width - pad.right) return;
    const outlier = Number(bucket.outlier_ratio) || 0;
    const personRatio = bucket.samples ? Number(bucket.person) / Number(bucket.samples) : 0;
    const barH = Math.max(1, outlier * plotHeight);
    const y = pad.top + plotHeight - barH;
    const isPicked = TIMELINE.picked && TIMELINE.picked.t === bucket.t;
    const isHover = TIMELINE.hoverIndex === index;
    if (isPicked) ctx.fillStyle = "#0f766e";
    else if (personRatio < 0.3) ctx.fillStyle = "#d3d1c7";
    else if (outlier >= 0.55) ctx.fillStyle = "#a32d2d";
    else if (outlier > 0) ctx.fillStyle = "#ba7517";
    else ctx.fillStyle = "#97c459";
    ctx.globalAlpha = isHover || isPicked ? 1 : 0.85;
    ctx.fillRect(x, y, bucketWidthPx, barH);
    ctx.globalAlpha = 1;
    if (isPicked) {
      ctx.strokeStyle = "#0f766e";
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 0.5, pad.top + 0.5, Math.max(1, bucketWidthPx - 1), plotHeight);
    }
  });

  // Time axis: 4 ticks across the window.
  ctx.fillStyle = "#75808e";
  ctx.font = "11px sans-serif";
  ctx.textBaseline = "top";
  const ticks = 4;
  for (let i = 0; i <= ticks; i += 1) {
    const ms = TIMELINE.start + (span * i) / ticks;
    const label = formatShortClock(new Date(ms).toISOString());
    const x = pad.left + (plotWidth * i) / ticks;
    ctx.textAlign = i === 0 ? "left" : i === ticks ? "right" : "center";
    ctx.fillText(label, x, pad.top + plotHeight + 6);
  }
}

function timelineBucketAtX(clientX) {
  const geo = timelineGeometry();
  if (!geo || !TIMELINE.buckets.length) return null;
  const rect = geo.canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  let best = null;
  TIMELINE.buckets.forEach((bucket, index) => {
    const t = parseTime(bucket.t);
    if (!Number.isFinite(t)) return;
    const bx = timelineXForTime(t, geo);
    const dx = Math.abs(bx - x);
    if (!best || dx < best.dx) best = { bucket, index, dx };
  });
  if (!best) return null;
  const bucketWidthPx = Math.max(2, (TIMELINE.bucketSeconds * 1000) * (geo.plotWidth / Math.max(1, TIMELINE.end - TIMELINE.start)));
  if (best.dx > Math.max(6, bucketWidthPx)) return null;
  return best;
}

function selectTimelineBucket(bucket) {
  TIMELINE.picked = bucket;
  const picked = $("timeline-picked");
  if (!bucket) {
    picked.hidden = true;
    drawTimeline();
    return;
  }
  picked.hidden = false;
  const at = parseTime(bucket.t);
  $("timeline-at").textContent = Number.isFinite(at)
    ? formatClock(bucket.t)
    : String(bucket.t);
  const outlier = Math.round((Number(bucket.outlier_ratio) || 0) * 100);
  const personPct = bucket.samples
    ? Math.round((Number(bucket.person) / Number(bucket.samples)) * 100)
    : 0;
  const up = bucket.head_up == null ? "—" : Number(bucket.head_up).toFixed(3);
  const fwd = bucket.head_fwd == null ? "—" : `${Number(bucket.head_fwd).toFixed(1)}°`;
  const granularity = TIMELINE.bucketSeconds >= 60
    ? `${TIMELINE.bucketSeconds / 60} 分钟段`
    : `${TIMELINE.bucketSeconds} 秒段`;
  $("timeline-detail").textContent =
    `${granularity} · 在场 ${personPct}% · 越线 ${outlier}% · 抬头 ${up} · 夹角 ${fwd} · 样本 ${bucket.samples}`;
  $("timeline-status").hidden = true;
  drawTimeline();
}

async function markTimelineMissed() {
  const bucket = TIMELINE.picked;
  if (!bucket) return;
  const status = $("timeline-status");
  status.hidden = false;
  status.textContent = "正在记录…";
  try {
    const response = await fetch("/api/missed-events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "add",
        at: bucket.t,
        issue: $("timeline-missed-issue").value,
        note: $("timeline-missed-note").value || "回看时间线标记",
        snapshot: {
          head_up: bucket.head_up,
          head_fwd: bucket.head_fwd,
          s_norm: bucket.s_norm,
        },
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    status.textContent = `已记录 ${formatClock(bucket.t)} 的漏报。`;
    $("timeline-missed-note").value = "";
    await loadMissed();
    loadTuning();
  } catch (error) {
    status.textContent = "记录失败：" + (error.message || "未知错误");
  }
}

function timelineZoom(factor, anchorClientX) {
  const geo = timelineGeometry();
  if (!geo) return;
  const rect = geo.canvas.getBoundingClientRect();
  const x = Math.min(Math.max(anchorClientX - rect.left, geo.pad.left), geo.width - geo.pad.right);
  const anchorTime = timelineTimeForX(x, geo);
  const span = TIMELINE.end - TIMELINE.start;
  let newSpan = span * factor;
  newSpan = Math.max(TIMELINE_MIN_SPAN_MS, Math.min(TIMELINE_MAX_SPAN_MS, newSpan));
  if (Math.abs(newSpan - span) < 1000) return;
  const ratio = (anchorTime - TIMELINE.start) / Math.max(1, span);
  TIMELINE.start = anchorTime - newSpan * ratio;
  TIMELINE.end = TIMELINE.start + newSpan;
  const now = Date.now() + (state.clockOffset || 0);
  if (TIMELINE.end > now) {
    const shift = TIMELINE.end - now;
    TIMELINE.end -= shift;
    TIMELINE.start -= shift;
  }
  loadTimeline();
}

function timelinePan(deltaPx) {
  const geo = timelineGeometry();
  if (!geo) return;
  const span = TIMELINE.end - TIMELINE.start;
  const deltaMs = (deltaPx / geo.plotWidth) * span;
  const now = Date.now() + (state.clockOffset || 0);
  let start = TIMELINE.start + deltaMs;
  let end = TIMELINE.end + deltaMs;
  if (end > now) {
    const shift = end - now;
    end -= shift;
    start -= shift;
  }
  TIMELINE.start = start;
  TIMELINE.end = end;
  loadTimeline();
}

function resetTimelineView() {
  const now = Date.now() + (state.clockOffset || 0);
  TIMELINE.end = now;
  TIMELINE.start = now - 24 * 60 * 60 * 1000;
  TIMELINE.picked = null;
  $("timeline-picked").hidden = true;
  loadTimeline();
}

function bindTimeline() {
  const canvas = $("timeline-chart");

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    const factor = event.deltaY > 0 ? 1.25 : 0.8;
    timelineZoom(factor, event.clientX);
  }, { passive: false });

  canvas.addEventListener("mousedown", (event) => {
    TIMELINE.dragging = true;
    TIMELINE.movedDuringDrag = false;
    TIMELINE.dragStartX = event.clientX;
    TIMELINE.dragStartWindow = { start: TIMELINE.start, end: TIMELINE.end };
    canvas.style.cursor = "grabbing";
  });

  window.addEventListener("mousemove", (event) => {
    if (!TIMELINE.dragging) return;
    const dx = event.clientX - TIMELINE.dragStartX;
    if (Math.abs(dx) > 3) TIMELINE.movedDuringDrag = true;
    if (!TIMELINE.movedDuringDrag) return;
    const geo = timelineGeometry();
    if (!geo) return;
    const span = TIMELINE.dragStartWindow.end - TIMELINE.dragStartWindow.start;
    const deltaMs = (dx / geo.plotWidth) * span;
    const now = Date.now() + (state.clockOffset || 0);
    let start = TIMELINE.dragStartWindow.start - deltaMs;
    let end = TIMELINE.dragStartWindow.end - deltaMs;
    if (end > now) {
      const shift = end - now;
      end -= shift;
      start -= shift;
    }
    TIMELINE.start = start;
    TIMELINE.end = end;
    drawTimeline();
    updateTimelineLabel();
  });

  window.addEventListener("mouseup", (event) => {
    if (!TIMELINE.dragging) return;
    const wasDrag = TIMELINE.movedDuringDrag;
    TIMELINE.dragging = false;
    canvas.style.cursor = "default";
    if (wasDrag) {
      loadTimeline();
    } else {
      const hit = timelineBucketAtX(event.clientX);
      if (hit) selectTimelineBucket(hit.bucket);
    }
  });

  canvas.addEventListener("mousemove", (event) => {
    if (TIMELINE.dragging) return;
    const hit = timelineBucketAtX(event.clientX);
    TIMELINE.hoverIndex = hit ? hit.index : null;
    canvas.style.cursor = hit ? "pointer" : "grab";
    drawTimeline();
  });

  canvas.addEventListener("mouseleave", () => {
    TIMELINE.hoverIndex = null;
    if (!TIMELINE.dragging) drawTimeline();
  });

  canvas.addEventListener("dblclick", resetTimelineView);

  // Touch: one finger pans, two fingers pinch to zoom.
  let pinchStart = null;
  canvas.addEventListener("touchstart", (event) => {
    if (event.touches.length === 1) {
      TIMELINE.dragging = true;
      TIMELINE.movedDuringDrag = false;
      TIMELINE.dragStartX = event.touches[0].clientX;
      TIMELINE.dragStartWindow = { start: TIMELINE.start, end: TIMELINE.end };
    } else if (event.touches.length === 2) {
      pinchStart = {
        distance: Math.abs(event.touches[0].clientX - event.touches[1].clientX),
        window: { start: TIMELINE.start, end: TIMELINE.end },
        mid: (event.touches[0].clientX + event.touches[1].clientX) / 2,
      };
      TIMELINE.dragging = false;
    }
  }, { passive: true });

  canvas.addEventListener("touchmove", (event) => {
    if (event.touches.length === 1 && TIMELINE.dragging) {
      const dx = event.touches[0].clientX - TIMELINE.dragStartX;
      if (Math.abs(dx) > 3) TIMELINE.movedDuringDrag = true;
      if (TIMELINE.movedDuringDrag) {
        const geo = timelineGeometry();
        if (geo) {
          const span = TIMELINE.dragStartWindow.end - TIMELINE.dragStartWindow.start;
          const deltaMs = (dx / geo.plotWidth) * span;
          const now = Date.now() + (state.clockOffset || 0);
          let start = TIMELINE.dragStartWindow.start - deltaMs;
          let end = TIMELINE.dragStartWindow.end - deltaMs;
          if (end > now) {
            const shift = end - now;
            end -= shift;
            start -= shift;
          }
          TIMELINE.start = start;
          TIMELINE.end = end;
          drawTimeline();
          updateTimelineLabel();
        }
        event.preventDefault();
      }
    } else if (event.touches.length === 2 && pinchStart) {
      const distance = Math.abs(event.touches[0].clientX - event.touches[1].clientX);
      if (pinchStart.distance > 20) {
        const factor = pinchStart.distance / Math.max(20, distance);
        const span = pinchStart.window.end - pinchStart.window.start;
        let newSpan = span * factor;
        newSpan = Math.max(TIMELINE_MIN_SPAN_MS, Math.min(TIMELINE_MAX_SPAN_MS, newSpan));
        TIMELINE.start = pinchStart.window.start;
        TIMELINE.end = pinchStart.window.start + newSpan;
        drawTimeline();
        updateTimelineLabel();
      }
      event.preventDefault();
    }
  }, { passive: false });

  canvas.addEventListener("touchend", () => {
    if (TIMELINE.dragging) {
      TIMELINE.dragging = false;
      if (TIMELINE.movedDuringDrag) loadTimeline();
      else {
        const hit = timelineBucketAtX(TIMELINE.dragStartX);
        if (hit) selectTimelineBucket(hit.bucket);
      }
    }
    pinchStart = null;
  });

  $("timeline-mark").addEventListener("click", markTimelineMissed);
  $("timeline-reset").addEventListener("click", resetTimelineView);
}

async function loadMissed() {
  try {
    const response = await fetch("/api/missed-events?hours=24", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    renderMissed(data.events || []);
  } catch (error) {
    console.error("missed load failed", error);
  }
}

async function missedRequest(body) {
  const status = $("missed-status");
  status.hidden = false;
  status.textContent = "正在保存…";
  try {
    const response = await fetch("/api/missed-events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    status.textContent = "已保存。";
    await loadMissed();
    loadTuning();
    return true;
  } catch (error) {
    status.textContent = "保存失败：" + (error.message || "未知错误");
    return false;
  }
}

function bindMissed() {
  const defaultAt = () => {
    const el = $("missed-at");
    if (!el.value) el.value = toLocalInput(Date.now());
  };
  defaultAt();
  $("missed-add").addEventListener("click", async () => {
    const raw = $("missed-at").value;
    if (!raw) {
      $("missed-status").hidden = false;
      $("missed-status").textContent = "请先选择时刻。";
      return;
    }
    const ok = await missedRequest({
      action: "add",
      at: fromLocalInput(raw).toISOString(),
      issue: $("missed-issue").value,
      note: $("missed-note").value,
    });
    if (ok) $("missed-note").value = "";
  });
  const list = $("missed-list");
  list.addEventListener("click", async (event) => {
    const item = event.target.closest(".missed-item");
    if (!item) return;
    const id = Number(item.dataset.id);
    if (event.target.closest(".missed-del")) {
      if (window.confirm("删除这条漏报标记？")) await missedRequest({ action: "delete", id });
      return;
    }
    if (event.target.closest(".missed-save")) {
      await missedRequest({
        action: "update",
        id,
        issue: item.querySelector(".missed-edit-issue").value,
        note: item.querySelector(".missed-edit-note").value,
      });
    }
  });
}

/* ------------------------------------------------------------------ */
/* Chart                                                               */
/* ------------------------------------------------------------------ */
function chartPointTime(point) {
  return parseTime(point.t);
}

function chartValues() {
  return state.series.filter((point) => Number.isFinite(point.hu) || Number.isFinite(point.hf));
}

function drawChart(hoverPoint) {
  const canvas = $("chart");
  const wrap = canvas.parentElement;
  const rect = wrap.getBoundingClientRect();
  if (!rect.width) return;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(rect.width * dpr);
  canvas.height = Math.round(rect.height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const width = rect.width;
  const height = rect.height;
  const pad = { left: 52, right: 16, top: 16, bottom: 24 };
  const gap = 18;
  const panelHeight = (height - pad.top - pad.bottom - gap) / 2;
  const panels = [
    { key: "hu", baseline: dataBaseline("head_up"), threshold: dataThreshold("head_up"), digits: 3, label: "抬头高度" },
    { key: "hf", baseline: dataBaseline("head_fwd"), threshold: dataThreshold("head_fwd"), digits: 1, label: "耳肩髋夹角" },
  ];

  const now = parseTime(state.data && state.data.now) || Date.now();
  const tEnd = now;
  const tStart = tEnd - CHART_MINUTES * 60 * 1000;
  const xOf = (ms) => pad.left + ((ms - tStart) / (tEnd - tStart)) * (width - pad.left - pad.right);

  ctx.clearRect(0, 0, width, height);
  ctx.font = '11px "PingFang SC", "Microsoft YaHei", system-ui, sans-serif';
  ctx.textBaseline = "middle";

  const points = chartValues();
  const hasData = points.length > 0;
  $("chart-empty").hidden = hasData;

  panels.forEach((panel, index) => {
    const top = pad.top + index * (panelHeight + gap);
    const bottom = top + panelHeight;

    const values = [];
    state.series.forEach((point) => {
      const value = point[panel.key];
      if (Number.isFinite(value)) values.push(Number(value));
    });
    if (Number.isFinite(panel.baseline)) values.push(panel.baseline);
    if (Number.isFinite(panel.threshold)) values.push(panel.threshold);
    const rawMin = values.length ? Math.min(...values) : 0;
    const rawMax = values.length ? Math.max(...values) : 1;
    const span = rawMax - rawMin || Math.max(0.05, Math.abs(rawMax) * 0.1 || 1);
    const padding = span * 0.18;
    const yMin = panel.key === "hu" ? Math.max(0, rawMin - padding) : rawMin - padding;
    const yMax = rawMax + padding;
    const yOf = (value) => bottom - ((value - yMin) / (yMax - yMin)) * (bottom - top);

    // Panel background and grid.
    ctx.fillStyle = "#fbfcfd";
    ctx.fillRect(pad.left, top, width - pad.left - pad.right, panelHeight);
    ctx.strokeStyle = "#eef1f5";
    ctx.lineWidth = 1;
    for (let tick = 0; tick <= 2; tick += 1) {
      const y = top + (panelHeight / 2) * tick;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      const value = yMax - ((yMax - yMin) / 2) * tick;
      ctx.fillStyle = "#98a2b3";
      ctx.textAlign = "right";
      ctx.fillText(
        panel.digits === 1 ? `${value.toFixed(0)}°` : value.toFixed(3),
        pad.left - 8,
        y
      );
    }

    ctx.fillStyle = "#98a2b3";
    ctx.textAlign = "left";
    ctx.fillText(panel.label, pad.left + 6, top + 10);

    // Baseline.
    if (Number.isFinite(panel.baseline)) {
      const y = yOf(panel.baseline);
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = "rgba(107, 119, 133, 0.55)";
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = "rgba(107, 119, 133, 0.85)";
      ctx.textAlign = "right";
      ctx.fillText("基线", width - pad.right - 2, y - 7);
    }

    // Threshold.
    if (Number.isFinite(panel.threshold)) {
      const y = yOf(panel.threshold);
      ctx.save();
      ctx.setLineDash([2, 3]);
      ctx.strokeStyle = "rgba(180, 35, 24, 0.55)";
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(width - pad.right, y);
      ctx.stroke();
      ctx.restore();
      ctx.fillStyle = "rgba(180, 35, 24, 0.8)";
      ctx.textAlign = "right";
      ctx.fillText("阈值", width - pad.right - 2, y + 9);
    }

    // Metric line, breaking on missing samples or long gaps.
    const color = panel.key === "hu" ? "#2563eb" : "#7c3aed";
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.beginPath();
    let previous = null;
    state.series.forEach((point) => {
      const value = point[panel.key];
      if (!Number.isFinite(value)) {
        previous = null;
        return;
      }
      const ms = chartPointTime(point);
      if (!Number.isFinite(ms)) return;
      const x = xOf(ms);
      const y = yOf(Number(value));
      if (previous && ms - previous.ms < 12_000) {
        ctx.lineTo(x, y);
      } else {
        ctx.moveTo(x, y);
      }
      previous = { ms };
    });
    ctx.stroke();

    // Outlier samples as small red dots on the line.
    ctx.fillStyle = "rgba(180, 35, 24, 0.55)";
    state.series.forEach((point) => {
      if (!point.issue || !Number.isFinite(point[panel.key])) return;
      const ms = chartPointTime(point);
      if (!Number.isFinite(ms) || ms < tStart) return;
      ctx.beginPath();
      ctx.arc(xOf(ms), yOf(Number(point[panel.key])), 1.7, 0, Math.PI * 2);
      ctx.fill();
    });
  });

  // Reminders: one vertical marker per event across both panels.
  const reminders = Array.isArray(state.data && state.data.reminders) ? state.data.reminders : [];
  ctx.save();
  reminders.forEach((reminder) => {
    const ms = parseTime(reminder.at);
    if (!Number.isFinite(ms) || ms < tStart || ms > tEnd) return;
    const x = xOf(ms);
    ctx.strokeStyle = "rgba(212, 58, 47, 0.38)";
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(x, pad.top);
    ctx.lineTo(x, height - pad.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "#d43a2f";
    ctx.beginPath();
    ctx.moveTo(x - 4, pad.top - 3);
    ctx.lineTo(x + 4, pad.top - 3);
    ctx.lineTo(x, pad.top + 4);
    ctx.closePath();
    ctx.fill();
  });
  ctx.restore();

  // Time axis.
  ctx.fillStyle = "#98a2b3";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  for (let tick = 0; tick <= 3; tick += 1) {
    const ms = tStart + ((tEnd - tStart) / 3) * tick;
    ctx.fillText(formatShortClock(ms), xOf(ms), height - pad.bottom + 7);
  }

  // Hover crosshair and marker dots.
  if (hoverPoint) {
    const ms = chartPointTime(hoverPoint);
    if (Number.isFinite(ms)) {
      const x = xOf(ms);
      ctx.strokeStyle = "rgba(24, 33, 43, 0.28)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, pad.top);
      ctx.lineTo(x, height - pad.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      panels.forEach((panel, index) => {
        const value = hoverPoint[panel.key];
        if (!Number.isFinite(value)) return;
        const top = pad.top + index * (panelHeight + gap);
        const bottom = top + panelHeight;
        const values = [];
        state.series.forEach((point) => {
          if (Number.isFinite(point[panel.key])) values.push(Number(point[panel.key]));
        });
        if (Number.isFinite(panel.baseline)) values.push(panel.baseline);
        if (Number.isFinite(panel.threshold)) values.push(panel.threshold);
        const rawMin = values.length ? Math.min(...values) : 0;
        const rawMax = values.length ? Math.max(...values) : 1;
        const span = rawMax - rawMin || Math.max(0.05, Math.abs(rawMax) * 0.1 || 1);
        const padding = span * 0.18;
        const yMin = panel.key === "hu" ? Math.max(0, rawMin - padding) : rawMin - padding;
        const yMax = rawMax + padding;
        const y = bottom - ((Number(value) - yMin) / (yMax - yMin)) * (bottom - top);
        ctx.fillStyle = panel.key === "hu" ? "#2563eb" : "#7c3aed";
        ctx.beginPath();
        ctx.arc(x, y, 3.4, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "#ffffff";
        ctx.beginPath();
        ctx.arc(x, y, 1.5, 0, Math.PI * 2);
        ctx.fill();
      });
    }
  }
}

function dataBaseline(key) {
  const baseline = state.data && state.data.scoring && state.data.scoring.baseline;
  if (!baseline) return null;
  const value = Number(baseline[key]);
  return Number.isFinite(value) ? value : null;
}

function dataThreshold(key) {
  const scoring = state.data && state.data.scoring;
  if (!scoring || !scoring.thresholds) return null;
  const value = Number(scoring.thresholds[key]);
  return Number.isFinite(value) ? value : null;
}

function seriesAtPixel(clientX) {
  const canvas = $("chart");
  const rect = canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  const pad = { left: 52, right: 16 };
  const now = parseTime(state.data && state.data.now) || Date.now();
  const tStart = now - CHART_MINUTES * 60 * 1000;
  const tEnd = now;
  const xOf = (ms) => pad.left + ((ms - tStart) / (tEnd - tStart)) * (rect.width - pad.left - pad.right);
  let best = null;
  state.series.forEach((point) => {
    const ms = chartPointTime(point);
    if (!Number.isFinite(ms)) return;
    if (!Number.isFinite(point.hu) && !Number.isFinite(point.hf)) return;
    const dx = Math.abs(xOf(ms) - x);
    if (!best || dx < best.dx) best = { point, dx, x: xOf(ms), ms };
  });
  if (!best || best.dx > 28) return null;
  return best;
}

function showChartTooltip(hit) {
  const tooltip = $("chart-tooltip");
  if (!hit) {
    tooltip.hidden = true;
    return;
  }
  const point = hit.point;
  const info = STATE_INFO[point.state] || { label: point.state };
  const rows = [
    ["抬头高度", Number.isFinite(point.hu) ? Number(point.hu).toFixed(3) : "—"],
    ["耳肩髋夹角", Number.isFinite(point.hf) ? `${Number(point.hf).toFixed(1)}°` : "—"],
    ["窗口越线", `${formatPercent(point.occ, 0)}%`],
    ["判定", info.label],
  ];
  tooltip.innerHTML = `<strong>${escapeHtml(formatClock(hit.ms))}</strong>${rows
    .map(
      ([label, value]) =>
        `<div class="tip-row"><span>${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></div>`
    )
    .join("")}`;
  tooltip.hidden = false;
  const wrap = $("chart").parentElement;
  const wrapRect = wrap.getBoundingClientRect();
  const left = Math.min(wrapRect.width - 90, Math.max(90, hit.x));
  tooltip.style.left = `${left}px`;
  tooltip.style.top = `${Math.max(70, 130)}px`;
}


/* ------------------------------------------------------------------ */
/* 24 hour review                                                      */
/* ------------------------------------------------------------------ */
function bucketSeconds() {
  const value = Number(state.history && state.history.bucket_seconds);
  return Number.isFinite(value) && value > 0 ? value : 300;
}

function historyGeometry() {
  const canvas = $("history-chart");
  const wrap = canvas && canvas.parentElement;
  if (!canvas || !wrap) return null;
  const rect = wrap.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const width = rect.width;
  const height = rect.height;
  const pad = { left: 52, right: 16, top: 16, bottom: 40 };
  const history = state.history;
  const fallbackEnd = parseTime(history && history.now) || Date.now();
  const hours = state.historyHours || [];
  const start = hours.length ? hours[0].start.getTime() : fallbackEnd - 24 * 60 * 60 * 1000;
  const end = hours.length ? hours[hours.length - 1].end.getTime() : fallbackEnd;
  const plotWidth = width - pad.left - pad.right;
  const slot = hours.length ? plotWidth / hours.length : plotWidth / 24;
  return {
    canvas,
    width,
    height,
    pad,
    start,
    end,
    slot,
    plotWidth,
  };
}

function drawHistoryChart(hover) {
  const geo = historyGeometry();
  if (!geo) return;
  const { canvas, width, height, pad, start, end, slot, plotWidth } = geo;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * dpr);
  canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const buckets = (state.history && state.history.buckets) || [];
  const rows = state.historyHours || [];
  $("history-empty").hidden = buckets.length > 0;
  if (!buckets.length || !rows.length) return;

  const plotLeft = pad.left;
  const plotRight = width - pad.right;
  const plotTop = pad.top + 8;
  const plotBottom = height - pad.bottom;
  const plotHeight = plotBottom - plotTop;
  const xOf = (ms) => plotLeft + ((ms - start) / (end - start)) * plotWidth;
  const slotFor = (index) => plotLeft + index * slot;

  ctx.font = '11px "PingFang SC", "Microsoft YaHei", system-ui, sans-serif';
  ctx.textBaseline = "middle";

  // Background grid for the hourly out-of-line ratio.
  for (let tick = 0; tick <= 4; tick += 1) {
    const y = plotBottom - (plotHeight / 4) * tick;
    ctx.strokeStyle = tick === 0 ? "#e6eaef" : "#eef1f5";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(plotLeft, y);
    ctx.lineTo(plotRight, y);
    ctx.stroke();
    ctx.fillStyle = "#98a2b3";
    ctx.textAlign = "right";
    ctx.fillText(`${tick * 25}%`, plotLeft - 8, y);
  }
  ctx.fillStyle = "#98a2b3";
  ctx.textAlign = "left";
  ctx.fillText("越线占比", plotLeft + 2, plotTop - 2);
  ctx.textAlign = "left";
  ctx.fillText("每根柱 = 1 小时", plotLeft + 78, plotTop - 2);
  ctx.fillText("灰块 = 屏蔽时段未检测", plotLeft + 178, plotTop - 2);

  // Hourly bars: height is the out-of-line fraction, opacity follows presence.
  rows.forEach((row, index) => {
    const x = slotFor(index);
    const barWidth = Math.max(4, slot * 0.62);
    const barLeft = x + (slot - barWidth) / 2;
    const hasData = row.samples > 0;
    const ratio = hasData ? Math.min(1, Math.max(0, row.outliers / row.samples)) : 0;
    const barHeight = hasData ? Math.max(ratio * plotHeight, ratio > 0 ? 3 : 1) : 0;

    if (!hasData) {
      // 屏蔽时段（上学日白天 / 夜间）：整列浅灰，明确区别于"有人但未越线"。
      // 只用一条 1px 底线会让人以为服务挂了。
      ctx.fillStyle = "rgba(120, 120, 120, 0.10)";
      ctx.fillRect(barLeft, plotTop, barWidth, plotHeight);
      if (slot >= 16) {
        ctx.save();
        ctx.fillStyle = "#b3b8c2";
        ctx.font = '10px "PingFang SC", "Microsoft YaHei", system-ui, sans-serif';
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.translate(barLeft + barWidth / 2, plotTop + plotHeight / 2);
        ctx.rotate(-Math.PI / 2);
        ctx.fillText("未检测", 0, 0);
        ctx.restore();
      }
      return;
    }

    if (row.person_ratio < 0.02) {
      ctx.fillStyle = "#e3e7ec";
    } else if (ratio >= 0.55) {
      ctx.fillStyle = "#c23a2b";
    } else if (ratio >= 0.3) {
      ctx.fillStyle = "#d97706";
    } else if (ratio > 0) {
      ctx.fillStyle = "#e0a23a";
    } else {
      ctx.fillStyle = "#7fb99b";
    }
    ctx.globalAlpha = 0.45 + 0.55 * Math.min(1, Math.max(0, row.person_ratio));
    ctx.fillRect(barLeft, plotBottom - barHeight, barWidth, barHeight);
    ctx.globalAlpha = 1;

    // A small red marker above the bar for each broadcasting hour.
    if (row.reminders > 0) {
      ctx.fillStyle = "#d43a2f";
      ctx.beginPath();
      ctx.arc(barLeft + barWidth / 2, Math.max(plotTop + 4, plotBottom - barHeight - 8), 3, 0, Math.PI * 2);
      ctx.fill();
    }

    // Presence underline: how much of the hour had a person.
    ctx.fillStyle = "rgba(15, 118, 110, 0.35)";
    ctx.fillRect(barLeft, plotBottom + 3, barWidth * Math.min(1, Math.max(0, row.person_ratio)), 2);
  });

  // Reminder markers along the top.
  const reminders = (state.history && state.history.reminders) || [];
  reminders.forEach((reminder) => {
    const ms = parseTime(reminder.at);
    if (!Number.isFinite(ms) || ms < start || ms > end) return;
    const x = xOf(ms);
    ctx.strokeStyle = "rgba(212, 58, 47, 0.3)";
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(x, plotTop);
    ctx.lineTo(x, plotBottom);
    ctx.stroke();
    ctx.setLineDash([]);
  });

  // Time axis labels: every three hours on desktop, six on a phone.
  ctx.fillStyle = "#98a2b3";
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  const labelStep = width < 700 ? 6 : 3;
  rows.forEach((row, index) => {
    if (index % labelStep !== 0 && index !== rows.length - 1) return;
    ctx.fillText(formatShortClock(row.start), slotFor(index) + slot / 2, plotBottom + 12);
  });

  if (hover && hover.start instanceof Date) {
    const index = rows.indexOf(hover);
    if (index >= 0) {
      const x = slotFor(index);
      ctx.fillStyle = "rgba(24, 33, 43, 0.05)";
      ctx.fillRect(x + 1, plotTop, Math.max(1, slot - 2), plotHeight);
      ctx.strokeStyle = "rgba(24, 33, 43, 0.25)";
      ctx.lineWidth = 1;
      ctx.strokeRect(x + 1, plotTop, Math.max(1, slot - 2), plotHeight);
    }
  }
}

function historyAtPixel(clientX) {
  const geo = historyGeometry();
  const rows = state.historyHours || [];
  if (!geo || !rows.length) return null;
  const rect = geo.canvas.getBoundingClientRect();
  const x = clientX - rect.left;
  let best = null;
  rows.forEach((row, index) => {
    const center = geo.pad.left + index * geo.slot + geo.slot / 2;
    const dx = Math.abs(center - x);
    if (!best || dx < best.dx) best = { row, dx, x: center, index };
  });
  if (!best || best.dx > geo.slot / 2 + 4) return null;
  return best;
}

function showHistoryTooltip(hit) {
  const tooltip = $("history-tooltip");
  if (!hit) {
    tooltip.hidden = true;
    return;
  }
  const row = hit.row;
  const personPct = row.samples ? `${Math.round((row.person / row.samples) * 100)}%` : "—";
  const outlierPct = row.samples
    ? `${Math.round((row.outliers / row.samples) * 100)}%`
    : "—";
  const rows = [
    ["样本", String(row.samples)],
    ["有人占比", personPct],
    ["越线占比", outlierPct],
    ["抬头均值", row.headUp == null ? "—" : Number(row.headUp).toFixed(3)],
    ["夹角均值", row.headFwd == null ? "—" : `${Number(row.headFwd).toFixed(1)}°`],
  ];
  if (row.reminders) rows.push(["播报", `${row.reminders} 次`]);
  tooltip.innerHTML = `<strong>${escapeHtml(formatShortClock(row.start))}–${escapeHtml(formatShortClock(row.end))}</strong>${rows
    .map(
      ([label, value]) =>
        `<div class="tip-row"><span>${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></div>`
    )
    .join("")}`;
  const wrap = $("history-chart").parentElement;
  const wrapRect = wrap.getBoundingClientRect();
  const left = Math.min(wrapRect.width - 90, Math.max(90, hit.x));
  tooltip.style.left = `${left}px`;
  tooltip.style.top = "100px";
  tooltip.hidden = false;
}

function hourKey(date) {
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}-${date.getHours()}`;
}

function aggregateHistoryHours(history) {
  const now = new Date(parseTime(history.now) || Date.now());
  const currentHour = new Date(now);
  currentHour.setMinutes(0, 0, 0);
  const bucketsByHour = new Map();
  (history.buckets || []).forEach((bucket) => {
    const date = new Date(parseTime(bucket.t));
    if (Number.isNaN(date.getTime())) return;
    const key = hourKey(date);
    if (!bucketsByHour.has(key)) bucketsByHour.set(key, []);
    bucketsByHour.get(key).push(bucket);
  });
  const remindersByHour = new Map();
  (history.reminders || []).forEach((reminder) => {
    const date = new Date(reminder.at);
    if (Number.isNaN(date.getTime())) return;
    const key = hourKey(date);
    remindersByHour.set(key, (remindersByHour.get(key) || 0) + 1);
  });
  const rows = [];
  for (let index = 23; index >= 0; index -= 1) {
    const start = new Date(currentHour);
    start.setHours(start.getHours() - index);
    const end = new Date(start);
    end.setHours(end.getHours() + 1);
    const items = bucketsByHour.get(hourKey(start)) || [];
    const samples = items.reduce((total, item) => total + item.samples, 0);
    const outliers = items.reduce((total, item) => total + item.outliers, 0);
    const person = items.reduce((total, item) => total + item.person, 0);
    const metricBuckets = items.filter((item) => item.head_up != null);
    const average = (key) =>
      metricBuckets.length
        ? metricBuckets.reduce((total, item) => total + Number(item[key]), 0) /
          metricBuckets.length
        : null;
    rows.push({
      start,
      end,
      samples,
      outliers,
      person,
      headUp: average("head_up"),
      headFwd: average("head_fwd"),
      reminders: remindersByHour.get(hourKey(start)) || 0,
      current: index === 0,
    });
  }
  return rows;
}

function renderHistoryTable(rows) {
  const activeRows = rows.filter((row) => row.samples > 0);
  if (!activeRows.length) {
    $("history-rows").innerHTML =
      '<tr class="is-empty"><td colspan="6">最近 24 小时还没有记录。</td></tr>';
    return;
  }
  $("history-rows").innerHTML = activeRows
    .map((row) => {
      const empty = row.samples === 0;
      const personPct = empty ? null : Math.round((row.person / row.samples) * 100);
      const outlierPct = empty ? null : Math.round((row.outliers / row.samples) * 100);
      const ratioClass =
        outlierPct == null ? "" : outlierPct >= 55 ? "is-high" : outlierPct >= 20 ? "is-mid" : "is-low";
      return `
        <tr class="${empty ? "is-empty" : ""} ${row.current ? "is-current" : ""}">
          <td class="time-cell">${escapeHtml(formatShortClock(row.start))}–${escapeHtml(formatShortClock(row.end))}${row.current ? '<span class="now-tag">现在</span>' : ""}</td>
          <td>${empty ? "—" : `${personPct}%`}</td>
          <td>${empty ? "—" : `<span class="ratio-chip ${ratioClass}">${outlierPct}%</span>`}</td>
          <td>${row.headUp == null ? "—" : row.headUp.toFixed(3)}</td>
          <td>${row.headFwd == null ? "—" : `${row.headFwd.toFixed(1)}°`}</td>
          <td>${row.reminders ? `<span class="count-badge">${row.reminders}</span>` : "—"}</td>
        </tr>`;
    })
    .join("");
}

function renderHistory() {
  const history = state.history;
  if (!history) return;
  const buckets = history.buckets || [];
  const summary = history.summary || {};
  const rows = aggregateHistoryHours(history);
  state.historyHours = rows;
  state.historyHover = null;
  $("history-tooltip").hidden = true;
  $("history-pill").textContent = `最近 ${Math.round(history.hours || 24)} 小时 · 按小时汇总`;
  $("history-empty").hidden = buckets.length > 0;
  if (!buckets.length) {
    $("history-summary").textContent = "最近 24 小时还没有记录。服务收到画面后，这里会按 5 分钟汇总。";
    $("history-rows").innerHTML = "";
    drawHistoryChart(null);
    renderAlerts();
    return;
  }
  const personMinutes = Math.round((summary.person_samples || 0) / 60);
  const personText =
    personMinutes >= 120
      ? `${(personMinutes / 60).toFixed(1)} 小时`
      : personMinutes >= 1
        ? `${personMinutes} 分钟`
        : "不足 1 分钟";
  const outlierPct = formatPercent(summary.outlier_ratio, 0);
  const outlierTone =
    summary.outlier_ratio >= 0.55 ? "is-bad" : summary.outlier_ratio >= 0.2 ? "is-warn" : "";
  $("history-summary").innerHTML = [
    `<span><strong>${Number(summary.samples || 0)}</strong> 个样本</span>`,
    `<span>在场约 <strong>${personText}</strong></span>`,
    `<span>越线占比 <strong class="${outlierTone}">${outlierPct}%</strong></span>`,
    `<span>播报 <strong>${Number(summary.reminders || 0)}</strong> 次</span>`,
    `<span class="muted">更新于 ${escapeHtml(formatClock(history.now))}</span>`,
  ].join("");
  renderHistoryTable(rows);
  drawHistoryChart(state.historyHover);
  drawTimeline();
  renderAlerts();
}

async function loadHistory() {
  if (state.historyLoading) return;
  state.historyLoading = true;
  try {
    const response = await fetch("/api/history?hours=24&bucket_seconds=300", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    state.history = data;
    const serverTime = parseTime(data.now);
    if (Number.isFinite(serverTime)) state.clockOffset = serverTime - Date.now();
    renderHistory();
  } catch (error) {
    console.error("history refresh failed", error);
    if (!state.history) {
      $("history-summary").textContent = "历史加载失败，稍后自动重试。";
      $("history-empty").hidden = true;
    }
  } finally {
    state.historyLoading = false;
  }
}

/* ------------------------------------------------------------------ */
/* Data flow                                                           */
/* ------------------------------------------------------------------ */
async function loadMonitor(full) {
  if (state.inFlight) return;
  state.inFlight = true;
  const params = new URLSearchParams({ minutes: String(CHART_MINUTES) });
  if (!full && state.lastT) params.set("since", state.lastT);
  try {
    const response = await fetch(`/api/monitor?${params.toString()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const previousLast = state.lastT;
    state.data = data;
    const serverTime = parseTime(data.now);
    if (Number.isFinite(serverTime)) {
      state.clockOffset = serverTime - Date.now();
    }
    if (full || !previousLast) {
      state.series = Array.isArray(data.series) ? data.series : [];
    } else {
      const incoming = Array.isArray(data.series) ? data.series : [];
      const known = new Set(state.series.map((point) => point.t));
      incoming.forEach((point) => {
        if (!known.has(point.t)) state.series.push(point);
      });
    }
    const cutoff = (parseTime(data.now) || Date.now()) - CHART_MINUTES * 60 * 1000;
    state.series = state.series.filter((point) => {
      const ms = chartPointTime(point);
      return !Number.isFinite(ms) || ms >= cutoff;
    });
    const last = state.series[state.series.length - 1];
    state.lastT = last ? last.t : null;
    state.failures = 0;
    renderAll();
  } catch (error) {
    state.failures += 1;
    setBadge("service-badge", "连接失败", "bad");
    $("updated-at").textContent = "正在重试…";
    console.error("monitor refresh failed", error);
  } finally {
    state.inFlight = false;
  }
}

function renderAll() {
  const data = state.data;
  if (!data) return;
  renderService(data);
  renderDecision(data);
  renderMetrics(data);
  renderWindow(data);
  renderActions(data);
  drawChart(state.hover);
  if (state.history) drawHistoryChart(state.historyHover);
}

/* ------------------------------------------------------------------ */
/* Wiring                                                              */
/* ------------------------------------------------------------------ */
function bindChart() {
  const canvas = $("chart");
  canvas.addEventListener("mousemove", (event) => {
    const hit = seriesAtPixel(event.clientX);
    if (hit) {
      state.hover = hit.point;
      showChartTooltip(hit);
    } else {
      state.hover = null;
      showChartTooltip(null);
    }
    drawChart(state.hover);
  });
  canvas.addEventListener("mouseleave", () => {
    state.hover = null;
    showChartTooltip(null);
    drawChart(null);
  });
  window.addEventListener("resize", () => drawChart(state.hover));
}

function startPolling() {
  if (state.timer) clearInterval(state.timer);
  state.timer = setInterval(() => {
    if (document.hidden) return;
    loadMonitor(false);
  }, REFRESH_MS);
}

function init() {
  renderClock();
  setInterval(renderClock, 1000);
  bindChart();
  bindVerdicts();
  bindMissed();
  bindTimeline();
  $("refresh-btn").addEventListener("click", () => {
    state.lastT = null;
    loadMonitor(true);
    loadHistory();
    loadMissed();
    loadTuning();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      loadMonitor(false);
      loadHistory();
    }
  });
  const historyCanvas = $("history-chart");
  historyCanvas.addEventListener("mousemove", (event) => {
    const hit = historyAtPixel(event.clientX);
    state.historyHover = hit ? hit.bucket : null;
    showHistoryTooltip(hit);
    drawHistoryChart(state.historyHover);
  });
  historyCanvas.addEventListener("mouseleave", () => {
    state.historyHover = null;
    showHistoryTooltip(null);
    drawHistoryChart(null);
  });
  window.addEventListener("resize", () => {
    drawChart(state.hover);
    drawHistoryChart(state.historyHover);
    drawTimeline();
  });
  loadMonitor(true);
  loadHistory();
  loadMissed();
  loadTuning();
  loadTimeline();
  setInterval(() => {
    if (!document.hidden) loadHistory();
  }, 60_000);
  setInterval(() => {
    // Refresh the timeline only when the user has not zoomed/panned away
    // from the live edge, so a deep look-back is never yanked out from under
    // them.
    if (document.hidden) return;
    const now = Date.now() + (state.clockOffset || 0);
    if (!TIMELINE.initialised || now - TIMELINE.end > 90_000) return;
    loadTimeline();
  }, 60_000);
  startPolling();
}

init();
