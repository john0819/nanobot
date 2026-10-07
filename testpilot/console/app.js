"use strict";
let bearer = "", controller = null, selected = "", approval = null;
const el = id => document.getElementById(id);
const show = (id, value) => { el(id).textContent = value == null ? "暂无" : JSON.stringify(value, null, 2); };
async function api(path, options = {}) {
  if (!bearer) throw new Error("请先连接身份");
  const response = await fetch(path, { ...options, headers: { Authorization: `Bearer ${bearer}`, "Content-Type": "application/json", ...options.headers }, cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
  return response.json();
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
async function details(path) {
  let state;
  try { state = await api(path); show("snapshot", state); } catch (error) { show("snapshot", error.message); }
  try { show("plan", await api(path + "/plan")); } catch { show("plan", null); }
  try { approval = await api(path + "/approval"); show("approval-detail", approval); } catch { approval = null; show("approval-detail", null); }
  el("artifacts").replaceChildren();
  try {
    const report = await api(path + "/report"); show("report", report);
    for (const hash of report.artifact_refs || []) {
      const button = document.createElement("button"); button.textContent = `下载证据 ${hash.slice(0, 12)}`;
      button.onclick = async () => {
        try {
          const response = await fetch(path + "/artifacts/" + hash, { headers: { Authorization: `Bearer ${bearer}` }, cache: "no-store" });
          if (!response.ok) throw new Error(`证据读取失败 ${response.status}`);
          const url = URL.createObjectURL(await response.blob()), link = document.createElement("a");
          link.href = url; link.download = hash + ".txt"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        } catch (error) { el("notice").textContent = error.message; }
      }; el("artifacts").append(button);
    }
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
action("connect", async () => { stop(); bearer = el("token").value.trim(); el("token").value = ""; await tasks(); });
action("logout", async () => { stop(); bearer = ""; selected = ""; approval = null; ["snapshot", "plan", "report", "approval-detail"].forEach(id => show(id, null)); ["tasks", "events", "artifacts"].forEach(id => el(id).replaceChildren()); el("token").value = ""; });
action("refresh", () => tasks()); action("review", () => tasks(true)); action("load", load);
action("create", async () => {
  const task = await api("/v1/tasks", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify({ mode: el("mode").value, goal: el("goal").value, require_approval: el("approval").checked, require_knowledge: el("knowledge").checked }) });
  el("task-id").value = task.task_id; await tasks(); await load();
});
action("cancel", async () => { const path = taskPath(); await api(path + "/cancel", { method: "POST" }); await details(path); });
for (const [id, decision] of [["approve", "APPROVE"], ["deny", "DENY"]]) action(id, async () => {
  const path = taskPath(); if (path !== selected || !approval) throw new Error("先查看当前任务的审批内容");
  await api(path + "/approval", { method: "POST", body: JSON.stringify({ decision, request_hash: approval.request_hash }) }); await details(path);
});
window.addEventListener("pagehide", stop);
