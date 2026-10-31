// worker/ask.js — /api/ask 처리기 (무료 Gemini, 대기열 없음: 한도 초과 시 429로 즉시 응답)
// 사용: worker/index.js 에서  import { handleAsk } from "./ask.js";
//       if (url.pathname === "/api/ask" && request.method === "POST") return handleAsk(request, env);
// 필요 설정: 비밀값 GEMINI_API_KEY (wrangler secret put GEMINI_API_KEY)
// 선택 변수: ASK_MODELS_FAST(쉼표 구분, 앞에서부터 시도. 기본 gemini-2.5-flash-lite,gemini-2.5-flash), ASK_RATE_PER_MIN(IP당 분당, 기본 8)
const API = "https://generativelanguage.googleapis.com";
const MAX_PDF = 45 * 1024 * 1024;
const SYSTEM = `당신은 한국 학생을 돕는 AI 튜터 "Unvul AI"입니다. 한국어로 정확하고 간결하게 답하세요.
- 사용자가 PDF를 골랐다면 그 PDF 내용을 근거로 답하고, 없는 내용은 지어내지 마세요. 질문이 가리키는 문제가 불분명하면 짧게 되물으세요.
- 풀이는 단계별로 하고 최종 답은 눈에 띄게 적으세요. 수식은 문장 속 $...$, 독립 수식 $$...$$ (LaTeX).
- "해설/정답 자료가 있는지"는 [자료실 목록]의 '판정'과 '같은 묶음' 줄만 근거로 답하세요. 목록에 없으면 "목록에는 없어요"라고 하세요.
- 목록의 자료를 언급할 땐 제목을 그대로 쓰세요. 확실하지 않으면 모른다고 하세요.`;

const hits = new Map(); // IP별 분당 호출 제한(Worker 인스턴스 메모리 기준의 대략적 제한)
function tooMany(ip, limit) {
  const now = Date.now(), arr = (hits.get(ip) || []).filter(t => now - t < 60000);
  arr.push(now); hits.set(ip, arr); if (hits.size > 5000) hits.clear();
  return arr.length > limit;
}
const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json; charset=utf-8" } });
const fail = (error, status, retryable = false) => json({ error, retryable }, status);

async function uploadPdf(downloadUrl, title, key) {
  const src = await fetch(downloadUrl, { redirect: "follow" });
  const size = Number(src.headers.get("content-length") || 0);
  if (!src.ok || !src.body) throw Object.assign(new Error("원본 PDF를 가져오지 못했어요."), { status: 502, retryable: true });
  if (!size || size > MAX_PDF) throw Object.assign(new Error("PDF가 너무 커서 AI가 읽을 수 없어요 (45MB 이하만 가능)."), { status: 413 });
  const start = await fetch(`${API}/upload/v1beta/files`, {
    method: "POST",
    headers: { "x-goog-api-key": key, "X-Goog-Upload-Protocol": "resumable", "X-Goog-Upload-Command": "start",
      "X-Goog-Upload-Header-Content-Length": String(size), "X-Goog-Upload-Header-Content-Type": "application/pdf", "Content-Type": "application/json" },
    body: JSON.stringify({ file: { display_name: String(title).slice(0, 80) } }),
  });
  const upUrl = start.headers.get("x-goog-upload-url");
  if (!start.ok || !upUrl) throw Object.assign(new Error("AI 서버에 파일을 올리지 못했어요."), { status: start.status === 429 ? 429 : 502, retryable: true });
  const { readable, writable } = new FixedLengthStream(size); // 스트리밍 전달: Worker CPU를 거의 쓰지 않음
  src.body.pipeTo(writable);
  const up = await fetch(upUrl, { method: "POST", headers: { "Content-Length": String(size), "X-Goog-Upload-Offset": "0", "X-Goog-Upload-Command": "upload, finalize" }, body: readable });
  const data = await up.json().catch(() => ({}));
  if (!up.ok || !data.file?.uri) throw Object.assign(new Error("AI 서버에 파일을 올리지 못했어요."), { status: 502, retryable: true });
  let f = data.file;
  for (let i = 0; i < 8 && f.state === "PROCESSING"; i++) { // PDF 처리 대기(보통 즉시 끝남)
    await new Promise(r => setTimeout(r, 1000));
    f = await (await fetch(`${API}/v1beta/${f.name}`, { headers: { "x-goog-api-key": key } })).json();
  }
  if (f.state === "FAILED") throw Object.assign(new Error("AI가 이 PDF를 처리하지 못했어요."), { status: 422 });
  return { uri: f.uri, mime: "application/pdf", exp: Date.now() + 40 * 3600 * 1000 }; // 보관 48시간 → 40시간만 재사용
}

async function generate(model, body, key) {
  const r = await fetch(`${API}/v1beta/models/${model}:generateContent`, { method: "POST", headers: { "x-goog-api-key": key, "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data: d };
}

export async function handleAsk(request, env) {
  const key = env.GEMINI_API_KEY;
  if (!key) return fail("AI 설정이 아직 안 끝났어요 (GEMINI_API_KEY 없음).", 500);
  if (tooMany(request.headers.get("cf-connecting-ip") || "unknown", Number(env.ASK_RATE_PER_MIN) || 8)) return fail("요청이 너무 잦아요. 잠시 후 다시 시도해주세요.", 429, true);
  let b; try { b = await request.json(); } catch { return fail("요청을 읽지 못했어요.", 400); }
  const question = String(b.question || "").slice(0, 3000);
  const atts = Array.isArray(b.attachments) ? b.attachments.slice(0, 4) : [];
  if (!question && !atts.length) return fail("질문을 입력해주세요.", 400);

  let ref = b.file_ref && b.file_ref.uri && b.file_ref.exp > Date.now() ? b.file_ref : null;
  const dl = b.file?.download_url;
  if (dl && !/^https:\/\/github\.com\//.test(dl)) return fail("허용되지 않은 자료 주소예요.", 400); // 임의 주소 요청 방지

  const history = (Array.isArray(b.history) ? b.history : []).slice(-10).map(m => ({ role: m.role === "assistant" ? "model" : "user", parts: [{ text: String(m.text || "").slice(0, 4000) }] }));
  const build = () => {
    const parts = [];
    if (ref) parts.push({ fileData: { fileUri: ref.uri, mimeType: ref.mime } });
    for (const a of atts) parts.push({ inlineData: { mimeType: a.mime, data: a.data } });
    parts.push({ text: `[자료실 목록]\n${String(b.manifest || "(없음)").slice(0, 4000)}\n\n[질문]\n${question || "첨부한 내용을 설명해줘."}` });
    return { systemInstruction: { parts: [{ text: SYSTEM }] }, contents: [...history, { role: "user", parts }], generationConfig: { temperature: 0.3, maxOutputTokens: 2048 } };
  };

  try {
    if (dl && !ref) ref = await uploadPdf(dl, b.file.title || "자료", key);
    const models = (env.ASK_MODELS_FAST || "gemini-2.5-flash-lite,gemini-2.5-flash").split(",").map(x => x.trim()).filter(Boolean);
    let res;
    for (const m of models) {
      res = await generate(m, build(), key);
      if (!res.ok && ref && b.file_ref && [400, 403, 404].includes(res.status)) { // 만료된 파일 참조: 한 번만 다시 업로드
        ref = await uploadPdf(dl, b.file.title || "자료", key); res = await generate(m, build(), key);
      }
      if (res.ok || ![429, 500, 503].includes(res.status)) break; // 한도/과부하일 때만 다음 모델로
    }
    if (!res.ok) {
      if (res.status === 429) return fail("지금 AI 무료 사용량이 가득 찼어요. 1분쯤 뒤에 다시 시도해주세요.", 429, true);
      if (res.status === 503) return fail("AI 서버가 잠시 바빠요. 다시 시도해주세요.", 503, true);
      return fail("AI 응답 오류가 났어요 (" + res.status + ").", 502, true);
    }
    const c = res.data.candidates?.[0];
    const answer = (c?.content?.parts || []).map(p => p.text || "").join("").trim();
    if (!answer) return fail("AI가 답을 만들지 못했어요. 질문을 바꿔보세요.", 502, true);
    return json({ answer, tier: "fast", file_ref: ref || undefined });
  } catch (e) {
    return fail(e.message || "알 수 없는 오류", e.status || 500, e.retryable ?? false);
  }
}
