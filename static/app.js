const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("file-input");
const browseBtn = document.getElementById("browse-btn");
const songList = document.getElementById("song-list");
const songEmpty = document.getElementById("song-empty");

const userInput = document.getElementById("user-input");
const overlapDuration = document.getElementById("overlap-duration");
const fadeDuration = document.getElementById("fade-duration");
const apiKeyInput = document.getElementById("api-key");
const apiKeyStatus = document.getElementById("api-key-status");
const saveKeyBtn = document.getElementById("save-key-btn");

const processBtn = document.getElementById("process-btn");
const logOutput = document.getElementById("log-output");

const resultCard = document.getElementById("result-card");
const resultAudio = document.getElementById("result-audio");
const resultWaveform = document.getElementById("result-waveform");
const resultPlanLink = document.getElementById("result-plan-link");

let pollTimer = null;

function formatSize(bytes) {
  if (bytes > 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
  return (bytes / 1024).toFixed(0) + " KB";
}

async function refreshSongs() {
  const res = await fetch("/api/songs");
  const songs = await res.json();
  songList.innerHTML = "";
  songEmpty.classList.toggle("hidden", songs.length > 0);
  for (const song of songs) {
    const li = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = song.name;
    const size = document.createElement("span");
    size.className = "song-size";
    size.textContent = formatSize(song.size);
    label.appendChild(size);

    const removeBtn = document.createElement("button");
    removeBtn.className = "remove-btn";
    removeBtn.textContent = "✕";
    removeBtn.title = "Verwijderen";
    removeBtn.addEventListener("click", async () => {
      await fetch(`/api/songs/${encodeURIComponent(song.name)}`, { method: "DELETE" });
      refreshSongs();
    });

    li.appendChild(label);
    li.appendChild(removeBtn);
    songList.appendChild(li);
  }
}

async function uploadFiles(fileListLike) {
  const files = Array.from(fileListLike).filter(f => f.name.toLowerCase().endsWith(".mp3"));
  if (files.length === 0) return;
  const formData = new FormData();
  for (const f of files) formData.append("files", f);
  await fetch("/api/upload", { method: "POST", body: formData });
  refreshSongs();
}

browseBtn.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => uploadFiles(fileInput.files));

["dragenter", "dragover"].forEach(evt =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("dragover");
  })
);
["dragleave", "drop"].forEach(evt =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove("dragover");
  })
);
dropzone.addEventListener("drop", (e) => {
  if (e.dataTransfer.files) uploadFiles(e.dataTransfer.files);
});

async function refreshSettings() {
  const res = await fetch("/api/settings");
  const data = await res.json();
  apiKeyStatus.textContent = data.has_api_key ? "(ingesteld ✓)" : "(niet ingesteld — fallback-modus)";
}

saveKeyBtn.addEventListener("click", async () => {
  const key = apiKeyInput.value.trim();
  if (!key) return;
  saveKeyBtn.disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: key }),
    });
    const data = await res.json();
    if (res.ok) {
      apiKeyInput.value = "";
      apiKeyStatus.textContent = "(ingesteld ✓)";
    } else {
      alert(data.error || "Opslaan mislukt");
    }
  } finally {
    saveKeyBtn.disabled = false;
  }
});

function setLogState(statusClass, text) {
  logOutput.classList.remove("status-error", "status-done");
  if (statusClass) logOutput.classList.add(statusClass);
  if (text !== undefined) logOutput.textContent = text;
}

function scrollLogToBottom() {
  logOutput.scrollTop = logOutput.scrollHeight;
}

async function pollJob(jobId) {
  const res = await fetch(`/api/status/${jobId}`);
  const data = await res.json();

  logOutput.textContent = data.log || "(nog geen output)";
  scrollLogToBottom();

  if (data.status === "done") {
    clearInterval(pollTimer);
    setLogState("status-done");
    processBtn.disabled = false;
    processBtn.textContent = "▶ Process";
    showResult(data.result);
  } else if (data.status === "error") {
    clearInterval(pollTimer);
    setLogState("status-error");
    processBtn.disabled = false;
    processBtn.textContent = "▶ Process";
  }
}

function showResult(result) {
  if (!result || !result.mix_url) return;
  resultCard.classList.remove("hidden");
  resultAudio.src = result.mix_url + "?t=" + Date.now();

  if (result.waveform_url) {
    resultWaveform.src = result.waveform_url + "?t=" + Date.now();
    resultWaveform.classList.remove("hidden");
  } else {
    resultWaveform.classList.add("hidden");
  }

  if (result.mixing_plan_url) {
    resultPlanLink.href = result.mixing_plan_url;
    resultPlanLink.classList.remove("hidden");
  } else {
    resultPlanLink.classList.add("hidden");
  }

  resultCard.scrollIntoView({ behavior: "smooth", block: "start" });
}

processBtn.addEventListener("click", async () => {
  resultCard.classList.add("hidden");
  setLogState(null, "Starten…");
  processBtn.disabled = true;
  processBtn.textContent = "⏳ Bezig…";

  const payload = {
    user_input: userInput.value || "Mix all songs",
    overlap_duration: parseFloat(overlapDuration.value) || 8.0,
    fade_duration: parseFloat(fadeDuration.value) || 1.0,
  };

  const res = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();

  if (!res.ok) {
    setLogState("status-error", data.error || "Starten mislukt");
    processBtn.disabled = false;
    processBtn.textContent = "▶ Process";
    return;
  }

  pollTimer = setInterval(() => pollJob(data.job_id), 1000);
});

refreshSongs();
refreshSettings();
