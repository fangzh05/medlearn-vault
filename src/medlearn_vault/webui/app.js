const state = { files: {}, output: "" };
const $ = (id) => document.getElementById(id);

let savedKey = false;
async function refreshKeyStatus() {
  const response = await fetch("/api/key/status");
  savedKey = (await response.json()).saved;
  $("keyStatus").textContent = savedKey ? "已安全保存（不会显示明文）" : "仅本次使用";
  $("clearKey").hidden = !savedKey;
}
refreshKeyStatus().catch(() => {});
$("clearKey").addEventListener("click", async () => {
  await fetch("/api/key/clear", { method: "POST" });
  await refreshKeyStatus();
});

async function readFile(input) {
  const file = input.files[0];
  if (!file) return null;
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return { name: file.name, content_base64: btoa(binary) };
}

function attachFileInput(id) {
  const input = $(id);
  if (!input) return;
  input.addEventListener("change", async () => {
    const file = await readFile(input);
    if (!file) return;
    state.files[id] = file;
    const name = input.closest(".dropzone, .file-row")?.querySelector("strong, .file-name");
    if (name) name.textContent = file.name;
  });
}

document.querySelectorAll(".dropzone, .file-row").forEach((row) => {
  row.addEventListener("click", (event) => {
    if (event.target.tagName !== "INPUT") row.querySelector("input")?.click();
  });
});
["intake", "template", "prompt", "index", "current_note", "golden_example"].forEach(attachFileInput);

$("composer").addEventListener("change", () => {
  const deep = $("composer").value === "deepseek";
  $("keyWrap").hidden = !deep;
  $("modeText").textContent = deep ? "DeepSeek · 显式联网模式" : "离线 stub 模式";
});

$("generate").addEventListener("click", async () => {
  const button = $("generate");
  const status = $("statusText");
  const issues = $("issues");
  issues.hidden = true;
  button.disabled = true;
  status.textContent = "正在准备输入…";
  try {
    if (!state.files.intake) throw Error("请选择 Intake JSON");
    const composer = $("composer").value;
    const payload = {
      intake: state.files.intake,
      template: state.files.template,
      prompt: state.files.prompt,
      current_note: state.files.current_note,
      golden_example: state.files.golden_example,
      index: state.files.index,
      composer,
      model: $("model").value,
      api_key: $("apiKey").value || null,
      remember_api_key: $("rememberKey").checked,
      use_saved_api_key: savedKey && !$("apiKey").value,
      retrieval_limit: 6,
    };
    if (composer === "deepseek" && !payload.prompt) throw Error("请选择 Prompt 文件");
    status.textContent = "正在生成…";
    const response = await fetch("/api/compose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (result.status === "rejected") {
      throw Error(result.error_code || result.blocker_codes?.join(", ") || "生成失败");
    }
    state.output = result.markdown || "";
    $("preview").textContent = state.output;
    $("previewTitle").textContent = "医学笔记预览";
    $("save").disabled = false;
    $("meta").hidden = false;
    $("meta").innerHTML = [
      `状态 ${result.status}`,
      `warnings ${result.warning_count}`,
      `retrieval ${result.retrieval_count}`,
      result.request_digest ? `request ${result.request_digest.slice(0, 18)}…` : "离线 stub",
      result.output_sha256 ? `output ${result.output_sha256.slice(0, 18)}…` : "",
    ].filter(Boolean).map((x) => `<span>${x}</span>`).join("");
    status.textContent = "生成完成";
    if (composer === "deepseek" && $("rememberKey").checked) await refreshKeyStatus();
  } catch (error) {
    status.textContent = "未生成";
    issues.innerHTML = `<strong>无法生成</strong><br>${String(error.message || error).replaceAll("<", "&lt;")}`;
    issues.hidden = false;
    $("previewTitle").textContent = "还没有生成笔记";
  } finally {
    // Keep the key out of browser storage and discard it as soon as this request ends.
    $("apiKey").value = "";
    button.disabled = false;
  }
});

$("save").addEventListener("click", () => {
  const blob = new Blob([state.output], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "medical-note.md";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
