// Web chat: send messages, follow the answers, countdown to the end of the conversation.
const chat = document.getElementById("chat");
const thread = document.getElementById("thread");
const draft = document.getElementById("draft");
const draftText = document.getElementById("draft-text");
const draftFrom = document.getElementById("draft-from");
const tool = document.getElementById("tool");
const status = document.getElementById("status");
const composer = document.getElementById("composer");
const field = document.getElementById("text");
const to = document.getElementById("to");  // missing if there are no colleagues to bring in
const buttons = Object.fromEntries(["stop", "pause", "resume", "end", "reopen", "new"].map(n => [n, document.getElementById(n)]));
// Texts in the language of the page.
const texts = JSON.parse(document.getElementById("chat-texts").textContent);
const say = (key, values = {}) => texts[key].replace(/\{(\w+)\}/g, (_, name) => values[name] ?? "");

// On a phone Enter makes a new line; sending is done with the button.
const touch = matchMedia("(pointer: coarse)").matches;

let conversation = chat.dataset.conversation;
let state = {state: chat.dataset.state, ends_at: parseFloat(chat.dataset.ends) || null};
let waitingFor = "";  // name of the colleague whose answer is pending
let refused = "";     // why the ended conversation could not be continued

function toBottom() {
  thread.scrollTop = thread.scrollHeight;
}

function lastId() {
  const all = thread.querySelectorAll(".message[data-id]");
  return all.length ? all[all.length - 1].dataset.id : 0;
}

function append(id, html) {
  if (document.getElementById(`m-${id}`)) return;
  document.getElementById("empty")?.remove();
  draft.insertAdjacentHTML("beforebegin", html);
  toBottom();
}

function clearDraft() {
  draft.hidden = true;
  draftText.textContent = "";
  tool.textContent = "";
}

// The draft belongs to whoever is working right now. That can also be an asked colleague.
function showDraft(from) {
  from = from || chat.dataset.name;
  if (draftFrom.textContent !== from) {
    draftText.textContent = "";
    draftFrom.textContent = from;
  }
  draft.hidden = false;
}

function show() {
  const s = state.state;
  let text = "";
  if (s === "running") text = say("writing", {name: draft.hidden && waitingFor ? waitingFor : chat.dataset.name});
  else if (s === "paused") text = say("paused");
  else if (s === "ending") text = say("ending");
  else if (s === "ended") text = refused || say("ended");
  else if (s === "open" && state.ends_at) {
    const rest = Math.max(0, Math.round(state.ends_at - Date.now() / 1000));
    text = say("ends_in", {m: Math.floor(rest / 60), s: String(rest % 60).padStart(2, "0")});
  }
  status.textContent = text;
  buttons.stop.hidden = s !== "running";
  buttons.pause.hidden = !(s === "open" && state.ends_at);
  buttons.resume.hidden = s !== "paused";
  buttons.end.hidden = !["open", "paused"].includes(s);
  buttons.reopen.hidden = s !== "ended";
  buttons.new.hidden = s !== "ended";
  composer.hidden = s === "ended";
  field.disabled = s === "ending";
  if (s !== "running") clearDraft();
}

function connect() {
  const source = new EventSource(`/conversation/${conversation}/events?after=${lastId()}`);
  // After reconnecting the service sends the whole partial text again.
  source.addEventListener("open", clearDraft);
  source.addEventListener("status", e => {
    state = JSON.parse(e.data);
    show();
    if (state.state === "ended") source.close();
  });
  source.addEventListener("delta", e => {
    const data = JSON.parse(e.data);
    showDraft(data.sender);
    tool.textContent = "";
    draftText.textContent += data.text;
    toBottom();
  });
  source.addEventListener("tool", e => {
    const data = JSON.parse(e.data);
    showDraft(data.sender);
    tool.textContent = say("using", {tool: data.name});
    toBottom();
  });
  source.addEventListener("message", e => {
    const m = JSON.parse(e.data);
    if (m.sender_kind === "agent") { clearDraft(); waitingFor = ""; }
    append(m.id, m.html);
  });
}

async function send() {
  const text = field.value.trim();
  if (!text) return;
  field.value = "";
  // A brought-in colleague answers once. After that the next message goes to the employee again.
  const colleague = to?.value || "";
  if (colleague) waitingFor = to.selectedOptions[0].dataset.name;
  if (to) to.value = "";
  const response = await fetch(`/chat/${chat.dataset.id}/message`, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({text, kind: chat.dataset.kind, to: colleague}),
  });
  const data = await response.json();
  if (!response.ok) {
    field.value = text;
    status.textContent = data.detail || say("send_failed");
    return;
  }
  append(data.id, data.html);
  if (!conversation) {
    conversation = data.conversation;
    connect();
  }
}

composer.addEventListener("submit", e => { e.preventDefault(); send(); });
field.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey && !touch) { e.preventDefault(); send(); }
});
// "stop" ends every run of the conversation. What was written so far stays as a message.
for (const action of ["stop", "pause", "resume", "end"]) {
  buttons[action].addEventListener("click", () => fetch(`/conversation/${conversation}/${action}`, {method: "POST"}));
}
// The note of an own change to the profile: undo it, or accept or discard a proposal. The note shows the new state.
thread.addEventListener("click", async e => {
  const button = e.target.closest("[data-profile-change]");
  if (!button) return;
  button.disabled = true;
  const response = await fetch(`/profile-change/${button.dataset.profileChange}/${button.dataset.action}`, {method: "POST"});
  const data = await response.json();
  if (!response.ok) { button.disabled = false; return alert(data.detail); }
  if (data.hint) alert(data.hint);
  if (data.html) document.getElementById(`m-${button.dataset.profileChange}`).outerHTML = data.html;
});
// The continued conversation is the ongoing one again. The button "New conversation" points to its page.
buttons.reopen.addEventListener("click", async () => {
  const response = await fetch(`/conversation/${conversation}/reopen`, {method: "POST"});
  if (response.ok) { location.href = buttons.new.href; return; }
  refused = (await response.json()).detail || say("reopen_failed");
  show();
});

setInterval(show, 1000);
show();
toBottom();
if (conversation && state.state !== "ended") connect();
