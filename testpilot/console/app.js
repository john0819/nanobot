"use strict";
let bearer = "", controller = null, selected = "", approval = null, snapshot = null;
let identityEpoch = 0;
const el = id => document.getElementById(id);
const show = (id, value) => { el(id).textContent = value == null ? "暂无" : JSON.stringify(value, null, 2); };
async function api(path, options = {}) {
  if (!bearer) throw new Error("请先连接身份");
  const epoch = identityEpoch;
  const response = await fetch(path, { ...options, headers: { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json", ...options.headers }, cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  const body = await response.json();
  if (epoch !== identityEpoch) throw new Error("身份已切换，已丢弃旧响应");
  return body;
}
function action(id, fn) { el(id).onclick = async () => { el("notice").textContent = ""; try { await fn(); } catch (error) { el("notice").textContent = error.message; } }; }
function stop() { controller?.abort(); controller = null; el("stream-status").textContent = "事件连接已关闭"; }
async function tasks(review = false) {
  const result = await api(`/v1/tasks?review=${review}`);
  el("tasks").replaceChildren();
  for (const task of result.tasks) {
    const button = document.createElement("button"); button.textContent = `${task.task_id} · ${task.state}`;
    button.onclick = () => { el("task-id").value = task.task_id; load().catch(error => { el("notice").textContent = error.message; }); };
    el("tasks").append(button);
  }
}
function taskPath() {
  const id = el("task-id").value.trim();
  if (!/^task_[0-9a-f]{32}$/.test(id)) throw new Error("任务 ID 格式不正确");
  return `/v1/tasks/${id}`;
}
function renderReport(report, path, runId = null) {
  show("report", report); el("artifacts").replaceChildren();
  for (const hash of report.artifact_refs || []) {
    const button = document.createElement("button"); button.textContent = `下载证据 ${hash.slice(0, 12)}`;
    button.onclick = async () => {
      const epoch = identityEpoch;
      try {
        const query = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
        const response = await fetch(path + "/artifacts/" + hash + query, { headers: { Authorization: `Bearer ${bearer}` }, cache: "no-store" });
        if (!response.ok) throw new Error(`证据读取失败 ${response.status}`);
        const blob = await response.blob(); if (epoch !== identityEpoch) return;
        const url = URL.createObjectURL(blob), link = document.createElement("a");
        link.href = url; link.download = hash + ".txt"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) { el("notice").textContent = error.message; }
    }; el("artifacts").append(button);
  }
}
async function details(path) {
  let state;
  try { state = await api(path); snapshot = state; show("snapshot", state); } catch (error) { snapshot = null; show("snapshot", error.message); }
  el("runs").replaceChildren();
  try {
    const history = await api(path + "/runs");
    for (const run of history.runs) {
      const button = document.createElement("button"); button.textContent = `Run ${run.sequence} · ${run.state} · ${run.quality_verdict || "待报告"}`;
      button.onclick = async () => { try { stop(); renderReport(await api(path + `/runs/${run.run_id}/report`), path, run.run_id); } catch (error) { el("notice").textContent = error.message; } };
      el("runs").append(button);
    }
  } catch { /* Reviewers can read approval requests without owning execution history. */ }
  try { show("plan", await api(path + "/plan")); } catch { show("plan", null); }
  try { approval = await api(path + "/approval"); show("approval-detail", approval); } catch { approval = null; show("approval-detail", null); }
  el("artifacts").replaceChildren();
  try {
    renderReport(await api(path + "/report"), path);
  } catch { show("report", null); }
  return state;
}
async function events(path, signal) {
  let cursor = 0;
  while (!signal.aborted) {
    try {
      const response = await fetch(path + `/events/stream?after=${cursor}`, { headers: { Authorization: `Bearer ${bearer}` }, signal, cache: "no-store" });
      if (response.status === 409) { cursor = 0; el("events").replaceChildren(); await details(path); continue; }
      if (!response.ok) throw new Error(`事件连接 ${response.status}`);
      el("stream-status").textContent = "事件已连接";
      const reader = response.body.getReader(), decoder = new TextDecoder(); let pending = "";
      while (!signal.aborted) {
        const { value, done } = await reader.read(); if (done) break;
        pending += decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = pending.indexOf("\n\n")) >= 0) {
          const frame = pending.slice(0, boundary); pending = pending.slice(boundary + 2);
          const id = frame.match(/^id: (\d+)$/m), data = frame.match(/^data: (.*)$/m);
          if (!data) continue;
          const event = JSON.parse(data[1]);
          if (id && Number(id[1]) > cursor) {
            cursor = Number(id[1]); const row = document.createElement("li"); row.textContent = JSON.stringify(event); el("events").append(row);
            while (el("events").children.length > 300) el("events").firstChild.remove();
          }
          if (event.resync_required) { cursor = 0; el("events").replaceChildren(); }
          if (id) {
            const state = await details(path);
            if (state && ["COMPLETED", "NEEDS_REVIEW", "CANCELLED"].includes(state.state)) { await reader.cancel(); el("stream-status").textContent = "任务已结束"; return; }
          }
        }
      }
    } catch (error) {
      if (signal.aborted) return;
      el("stream-status").textContent = error.message;
      if (/401|403|404/.test(error.message)) return;
    }
    await new Promise(resolve => { const timer = setTimeout(resolve, 1500); signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true }); });
  }
}
async function load() {
  stop(); selected = taskPath(); el("events").replaceChildren(); await details(selected);
  controller = new AbortController(); events(selected, controller.signal).catch(error => { el("notice").textContent = error.message; });
}
function clearIdentity() {
  stop(); identityEpoch++; bearer = ""; selected = ""; approval = null; snapshot = null;
  ["snapshot", "plan", "report", "approval-detail"].forEach(id => show(id, null));
  ["tasks", "events", "artifacts", "runs", "memories"].forEach(id => el(id).replaceChildren());
  ["task-id", "source-run", "source-case"].forEach(id => { el(id).value = ""; });
}
action("connect", async () => { const token = el("token").value.trim(); clearIdentity(); bearer = token; el("token").value = ""; await tasks(); });
action("logout", async () => { clearIdentity(); el("token").value = ""; });
action("refresh", () => tasks()); action("review", () => tasks(true)); action("load", load);
action("create", async () => {
  const task = await api("/v1/tasks", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ mode: el("mode").value, goal: el("goal").value, require_approval: el("approval").checked, require_knowledge: el("knowledge").checked }) });
  el("task-id").value = task.task_id; await tasks(); await load();
});
action("cancel", async () => { const path = taskPath(); await api(path + "/cancel", { method: "POST" }); await details(path); });
action("rerun", async () => {
  const path = taskPath(); if (path !== selected || !snapshot) throw new Error("先查看当前任务");
  await api(path + "/rerun", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ expected_state_version: snapshot.state_version, reason: el("rerun-reason").value }) }); await load();
});
async function memories(review = false) {
  const result = await api(`/v1/memory?review=${review}`); el("memories").replaceChildren();
  for (const item of result.records) {
    const block = document.createElement("div"), text = document.createElement("pre"); text.textContent = JSON.stringify(item, null, 2); block.append(text);
    for (const decision of ["CONFIRM", "REVOKE"]) {
      const button = document.createElement("button"); button.textContent = decision === "CONFIRM" ? "确认经验" : "撤销经验";
      button.onclick = async () => { try { await api(`/v1/memory/${item.id}/decision`, { method: "POST", body: JSON.stringify({ expected_version: item.version, decision }) }); await memories(review); } catch (error) { el("notice").textContent = error.message; } }; block.append(button);
    } el("memories").append(block);
  }
}
action("memory-list", () => memories()); action("memory-review", () => memories(true));
action("memory-create", async () => {
  await api(taskPath() + "/memory", { method: "POST", body: JSON.stringify({ source_run_id: el("source-run").value.trim(), case_id: el("source-case").value.trim(), shared: el("memory-shared").checked }) }); await memories();
});
for (const [id, decision] of [["approve", "APPROVE"], ["deny", "DENY"]]) action(id, async () => {
  const path = taskPath(); if (path !== selected || !approval) throw new Error("先查看当前任务的审批内容");
  await api(path + "/approval", { method: "POST", body: JSON.stringify({ decision, request_hash: approval.request_hash }) }); await details(path);
});
window.addEventListener("pagehide", stop);
