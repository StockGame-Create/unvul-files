// api/ask.js  (v2: 파일 선택 없이 바로 질문 / 사진·PDF 첨부 / 문제 은행 참조)
//
// 흐름
// 1) 사용자가 질문(+선택: 사진/PDF 첨부)을 보낸다. 더 이상 "자료 선택"은 없다.
// 2) 사진/PDF가 있으면 가벼운 모델로 "문제 본문/과목/단원"만 먼저 뽑는다 (검색어용).
// 3) ai_power.py가 만들어 둔 sites/bank_index.json(문제 은행 검색 색인)에서
//    비슷한 문제를 찾는다 (한글에 강한 글자 2-gram 검색, 형태소 분석기 불필요).
//    - 거의 같은 문제가 있고 해설이 있으면 그 해설 전문을 개별 json에서 가져와 근거로 쓴다.
//    - 나머지 비슷한 문제는 "추천 문제"로 응답에 같이 내려준다 (프론트가 카드로 표시).
// 4) 첨부 + 참고 자료 + 대화 기록을 Gemini에 보내 답을 받는다.
//    mode = "fast"(기본) 는 Flash 계열, "deep" 은 Pro 계열 (ASK_MODELS_DEEP 설정 시).
//    deep 모델이 한도/혼잡으로 실패하면 fast 모델로 자동 대체하고 그 사실을 알려준다.
//
// 이 함수는 상태를 저장하지 않는다 (history와 첨부는 클라이언트가 매번 다시 보냄).
//
// 환경변수 (Vercel > Settings > Environment Variables)
//   GEMINI_API_KEY          (필수)
//   ASK_MODELS_FAST         쉼표 구분, 앞에서부터 시도 (기본: gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash)
//   ASK_MODELS_DEEP         Pro 계열 모델 목록. 비워두면 deep 요청도 fast로 처리하고 알려준다.
//   ASK_DEEP_PER_HOUR       IP당 시간당 deep 허용 횟수 (기본 5, 서버리스 인스턴스 메모리 기준의 "대충" 제한)
//   ASK_RATE_PER_MIN        IP당 분당 요청 수 (기본 8)
//
// 요청: POST {
//   question: string,
//   history?: [{ role: "user"|"assistant", text }],
//   attachments?: [{ mime: "image/jpeg"|"image/png"|"image/webp"|"application/pdf", data: base64 }],
//   mode?: "fast" | "deep"
// }
// 응답: { answer, related: [...], tier: "fast"|"deep", fell_back: boolean, retrieval: "same"|"similar"|"none" }
//       또는 { error, retryable? }
//
// 주의: Vercel Hobby의 요청 본문 한도는 약 4.5MB다. 프론트가 사진을 줄여서 보내고,
// 여기서도 총량을 검사해 넘으면 안내 메시지를 돌려준다.

const GITHUB_OWNER = "StockGame-Create"; // index.html / view.js와 동일하게 유지
const GITHUB_REPO = "unvul-files";
const GITHUB_BRANCH = "main";
const RAW_BASE = `https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/${GITHUB_BRANCH}/sites/`;

function envList(name, def) {
  const v = (process.env[name] || "").trim() || def;
  return v.split(",").map((s) => s.trim()).filter(Boolean);
}
function envInt(name, def) {
  const n = parseInt((process.env[name] || "").trim(), 10);
  return Number.isFinite(n) ? n : def;
}

const FAST_MODELS = envList("ASK_MODELS_FAST", "gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash");
const DEEP_MODELS = envList("ASK_MODELS_DEEP", "");
const DEEP_PER_HOUR = envInt("ASK_DEEP_PER_HOUR", 5);
const RATE_PER_MIN = envInt("ASK_RATE_PER_MIN", 8);

const MAX_QUESTION_LENGTH = 2000;
const MAX_HISTORY_TURNS = 30;
const MAX_ATTACHMENTS = 4;
const MAX_ATTACH_BASE64_TOTAL = 3_800_000; // Vercel 본문 한도(4.5MB) 안쪽으로
const ALLOWED_MIME = new Set(["image/jpeg", "image/png", "image/webp", "application/pdf"]);

const TOTAL_TIME_BUDGET_MS = 52000; // maxDuration(60초) 안에서 끝내기 위한 전체 예산
const EXTRACT_TIME_BUDGET_MS = 12000; // 검색어 추출 단계는 이만큼만 쓴다 (실패해도 무시하고 진행)

// ---- 문제 은행 검색 -----------------------------------------------------
const BANK_TTL_MS = 10 * 60 * 1000;
const MIN_QUERY_BIGRAMS = 6; // 이보다 짧은 질문은 검색하지 않는다 ("풀어줘" 같은 것)
const SAME_THRESHOLD = 0.6; // Dice 유사도가 이 이상이면 "같은 문제"로 본다 (실제 자료로 보며 조정)
const RELATED_MIN = 0.14; // 이 미만이면 추천에서 제외 (잡음 방지)
const RELATED_MAX = 5;
const DETAIL_FETCH_MAX = 2; // 해설 전문을 가져올 상위 개수
const COMMON_BIGRAM_RATIO = 0.25; // 문서의 25% 이상에 나오는 2-gram은 후보 생성에서 제외

let bankCache = { at: 0, bank: null };

function normalizeForSearch(str) {
  return String(str || "")
    .normalize("NFC")
    .toLowerCase()
    .replace(/[\s$\\{}(),.;:'"\[\]<>|·ㆍ]/g, "");
}

function bigramSet(str) {
  const s = normalizeForSearch(str);
  const set = new Set();
  for (let i = 0; i < s.length - 1; i++) set.add(s.slice(i, i + 2));
  return set;
}

async function loadBank() {
  if (bankCache.bank && Date.now() - bankCache.at < BANK_TTL_MS) return bankCache.bank;
  try {
    const res = await fetch(RAW_BASE + "bank_index.json", { cache: "no-store", signal: AbortSignal.timeout(10000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    const items = data.items || [];
    const postings = new Map(); // 2-gram -> 문항 인덱스 배열
    items.forEach((it, i) => {
      for (const bg of bigramSet(`${it.k || ""} ${it.t || ""}`)) {
        let arr = postings.get(bg);
        if (!arr) postings.set(bg, (arr = []));
        arr.push(i);
      }
    });
    bankCache = { at: Date.now(), bank: { files: data.files || [], items, postings } };
    return bankCache.bank;
  } catch (err) {
    console.warn("[문제 은행 로드 실패]", err.message);
    return bankCache.bank; // 오래된 캐시라도 있으면 그걸 쓴다 (없으면 null -> 검색 생략)
  }
}

function searchBank(bank, queryText, subjectHint) {
  const qSet = bigramSet(queryText);
  if (!bank || qSet.size < MIN_QUERY_BIGRAMS) return [];

  const N = bank.items.length;
  const commonCut = N >= 200 ? N * COMMON_BIGRAM_RATIO : Infinity; // 문항이 적을 땐 가지치기하지 않는다
  const acc = new Map();
  for (const bg of qSet) {
    const arr = bank.postings.get(bg);
    if (!arr || arr.length > commonCut) continue;
    const idf = Math.log(1 + N / arr.length);
    for (const i of arr) acc.set(i, (acc.get(i) || 0) + idf);
  }

  const candidates = [...acc.entries()].sort((a, b) => b[1] - a[1]).slice(0, 40);
  const hint = normalizeForSearch(subjectHint);

  const scored = candidates.map(([i]) => {
    const it = bank.items[i];
    const dSet = bigramSet(`${it.k || ""} ${it.t || ""}`);
    let inter = 0;
    for (const bg of qSet) if (dSet.has(bg)) inter++;
    let dice = (2 * inter) / (qSet.size + dSet.size || 1);
    const file = bank.files[it.f] || {};
    if (hint && normalizeForSearch(file.subject).includes(hint)) dice *= 1.1;
    return { it, file, score: Math.min(dice, 1) };
  });

  return scored.sort((a, b) => b.score - a.score).filter((x) => x.score >= RELATED_MIN).slice(0, RELATED_MAX);
}

// 상위 문항의 전체 본문/해설을 파일별 json에서 가져온다 (실패해도 조용히 무시).
async function fetchDetails(hits) {
  await Promise.all(
    hits.slice(0, DETAIL_FETCH_MAX).map(async (h) => {
      if (!h.file.id) return;
      try {
        const res = await fetch(`${RAW_BASE}ai/${h.file.id}.json`, { signal: AbortSignal.timeout(8000) });
        if (!res.ok) return;
        const data = await res.json();
        const p = (data.problems || []).find((x) => x.number === h.it.n && x.page === h.it.p);
        if (p) h.detail = p;
      } catch {
        /* 상세 조회 실패는 무시: 색인에 있는 요약만으로 진행 */
      }
    })
  );
}

function buildReferenceText(hits) {
  if (!hits.length) return "";
  const lines = [
    "[자료실 참고 자료] 아래는 사용자 자료실의 PDF에서 AI가 자동 추출한 문제/해설입니다. OCR 오류가 있을 수 있으니 맹신하지 말고, 질문과 실제로 관련 있을 때만 근거로 쓰세요.",
  ];
  hits.slice(0, 3).forEach((h, idx) => {
    const p = h.detail;
    const label = h.score >= SAME_THRESHOLD ? "같은 문제일 가능성이 높음" : "비슷한 문제";
    lines.push(
      `\n(${idx + 1}) ${label} · 유사도 ${(h.score * 100).toFixed(0)}% · 출처: ${h.file.title || "자료"} (${[h.file.subject, h.file.year].filter(Boolean).join(", ")}) p.${h.it.p} ${h.it.n}번`
    );
    lines.push(`문제: ${(p && p.text) || h.it.t}`);
    if (p && p.choices && p.choices.length) lines.push(`선택지: ${p.choices.map((c, i) => `${i + 1}) ${c}`).join(" / ")}`);
    const ans = (p && p.answer) || h.it.a;
    if (ans) lines.push(`정답(자료 표기): ${ans}`);
    if (p && p.explanation) lines.push(`해설(자료): ${p.explanation.slice(0, 3000)}`);
  });
  return lines.join("\n");
}

// ---- Gemini 호출 --------------------------------------------------------
const SYSTEM_INSTRUCTION = `당신은 한국 학생을 돕는 AI 튜터 "Unvul AI"입니다.

규칙:
1) 사용자가 사진이나 PDF를 첨부하면 그 안의 문제를 먼저 정확히 읽고, 질문에 맞게 풀이/설명하세요. 여러 문제가 있으면 질문에서 가리킨 문제를 우선하고, 불분명하면 어떤 문제를 말하는지 짧게 되물으세요.
2) "[자료실 참고 자료]"가 주어지면 우선 확인하세요. '같은 문제일 가능성이 높음'이고 해설이 있으면 그 해설을 근거로 설명하되, 직접 계산해서 해설/정답과 다르면 그 사실을 숨기지 말고 어느 쪽이 맞아 보이는지 이유와 함께 알려주세요. 참고 자료를 썼다면 출처(자료 제목, 페이지, 번호)를 한 줄로 밝히세요.
3) 풀이는 단계별로, 핵심 개념을 먼저 짚고 계산 과정을 보여 주세요. 최종 답은 눈에 띄게 적으세요.
4) 수식은 LaTeX로 쓰세요: 문장 속은 $...$, 독립 수식은 $$...$$.
5) 마크다운(목록, 굵게, 표)을 필요한 곳에만 적절히 쓰세요.
6) 확실하지 않으면 추측으로 단정하지 말고 모른다고 말하세요. 이미지가 흐리거나 잘려서 못 읽겠다면 그렇게 말하고 다시 올려 달라고 하세요.
7) 이전 대화 맥락을 기억하고 자연스럽게 이어서 답하세요.
8) 한국어로, 정확하고 간결하게 답하세요. 사용자가 "비슷한 문제"를 요청하면 직접 새 문제를 만들어 주되 정답과 풀이를 함께 제공하세요.`;

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

async function callModelOnce(model, payload, deadlineAt) {
  const remaining = Math.max(1000, deadlineAt - Date.now());
  let res;
  try {
    res = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": process.env.GEMINI_API_KEY },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(remaining),
    });
  } catch (err) {
    return { ok: false, network: true, detail: err.message };
  }
  if (res.ok) return { ok: true, data: await res.json() };

  const raw = await res.text().catch(() => "");
  const m = raw.match(/"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"/);
  return {
    ok: false,
    status: res.status,
    // 일일 한도는 기다려도 안 풀리므로 구분한다 (예: GenerateRequestsPerDayPerProjectPerModel).
    daily: /perday/i.test(raw.replace(/[\s_]/g, "")),
    retryAfter: m ? parseFloat(m[1]) : null,
    detail: raw.slice(0, 300),
  };
}

// 모델 목록을 "순서대로" 시도한다 (병렬 레이스는 쿼터를 여러 배로 태워서 쓰지 않는다).
async function generate(models, payload, deadlineAt) {
  let lastDetail = "";
  let sawOverload = false;

  for (const model of models) {
    for (let attempt = 1; attempt <= 2; attempt++) {
      if (Date.now() >= deadlineAt - 2000) {
        const err = new Error("시간이 너무 오래 걸려서 중단했어요. 잠시 후 다시 시도해주세요.");
        err.statusCode = 504;
        err.retryable = true;
        throw err;
      }
      const r = await callModelOnce(model, payload, deadlineAt);
      if (r.ok) return { model, data: r.data };

      lastDetail = `${model}: ${r.status || "네트워크"} ${r.detail || ""}`;
      console.warn("[Gemini 실패]", lastDetail);

      if (r.status === 401 || r.status === 403) {
        const err = new Error("서버의 Gemini API 키가 올바르지 않거나 권한이 없습니다.");
        err.statusCode = 500;
        throw err;
      }
      if (r.status === 404) break; // 없는 모델 -> 다음 모델
      if (r.status === 429 && r.daily) {
        sawOverload = true;
        break; // 이 모델은 오늘 소진 -> 다음 모델
      }
      if (r.status === 429 || r.status === 500 || r.status === 502 || r.status === 503 || r.status === 504 || r.network) {
        sawOverload = true;
        const wait = r.retryAfter != null ? r.retryAfter * 1000 : 1500 * attempt;
        if (attempt < 2 && wait <= 8000 && Date.now() + wait < deadlineAt - 8000) {
          await sleep(wait);
          continue;
        }
        break; // 대기가 너무 길다 -> 다음 모델
      }
      // 400 등: 요청 자체 문제라 다른 모델로 가도 같다.
      const err = new Error("AI 요청을 처리하지 못했어요. 첨부한 파일 형식/크기를 확인해주세요.");
      err.statusCode = 400;
      throw err;
    }
  }

  const err = new Error(
    sawOverload
      ? "지금 AI 사용량이 많거나 오늘 한도에 도달했어요. 잠시 후(또는 내일) 다시 시도해주세요."
      : `AI가 응답하지 못했어요. (${lastDetail.slice(0, 120)})`
  );
  err.statusCode = 503;
  err.retryable = true;
  throw err;
}

function textOf(data) {
  return data?.candidates?.[0]?.content?.parts?.map((p) => p.text || "").join("") || "";
}

// 첨부에서 검색에 쓸 문제 본문/과목/단원을 뽑는다. 실패하면 null (검색 없이 계속 진행).
async function extractQuery(attachments) {
  if (!attachments.length) return null;
  const payload = {
    system_instruction: { parts: [{ text: "첨부된 학습 자료 이미지/PDF에서 첫 번째 문제를 그대로 전사한다. JSON으로만 답한다." }] },
    contents: [
      {
        role: "user",
        parts: [
          ...attachments.map((a) => ({ inline_data: { mime_type: a.mime, data: a.data } })),
          { text: "첫 번째(또는 가장 눈에 띄는) 문제의 본문과 선택지를 전사하고, 과목과 단원을 짧게 적어줘. 수식은 LaTeX($...$)로. 문제가 없으면 problem_text를 빈 문자열로." },
        ],
      },
    ],
    generationConfig: {
      temperature: 0,
      maxOutputTokens: 1500,
      responseMimeType: "application/json",
      responseSchema: {
        type: "OBJECT",
        properties: {
          problem_text: { type: "STRING" },
          subject: { type: "STRING" },
          topic: { type: "STRING" },
        },
        required: ["problem_text"],
      },
    },
  };
  try {
    const { data } = await generate(FAST_MODELS, payload, Date.now() + EXTRACT_TIME_BUDGET_MS);
    return JSON.parse(textOf(data));
  } catch (err) {
    console.warn("[검색어 추출 생략]", err.message);
    return null;
  }
}

// ---- 대충의 호출 제한 (서버리스라 인스턴스 메모리 기준 - 완벽하지 않음) ----
const rateHits = new Map();
const deepHits = new Map();

function hit(map, ip, windowMs, limit) {
  const now = Date.now();
  const arr = (map.get(ip) || []).filter((t) => now - t < windowMs);
  arr.push(now);
  map.set(ip, arr);
  if (map.size > 5000) map.clear();
  return arr.length > limit;
}

function clientIp(req) {
  return String(req.headers["x-forwarded-for"] || req.socket?.remoteAddress || "unknown").split(",")[0].trim();
}

// ---- 핸들러 ---------------------------------------------------------------
module.exports = async function handler(req, res) {
  const startedAt = Date.now();

  if (req.method !== "POST") {
    res.status(405).json({ error: "POST만 지원합니다." });
    return;
  }
  if (!process.env.GEMINI_API_KEY) {
    res.status(500).json({ error: "서버에 GEMINI_API_KEY가 설정돼있지 않습니다. (Vercel 환경변수 확인 필요)" });
    return;
  }

  const ip = clientIp(req);
  if (hit(rateHits, ip, 60_000, RATE_PER_MIN)) {
    res.status(429).json({ error: "요청이 너무 잦아요. 잠시 후 다시 시도해주세요.", retryable: true });
    return;
  }

  const { question, history, attachments, mode } = req.body || {};
  const trimmedQuestion = typeof question === "string" ? question.trim() : "";

  let safeAttachments = [];
  if (Array.isArray(attachments)) {
    safeAttachments = attachments
      .filter((a) => a && typeof a.data === "string" && ALLOWED_MIME.has(a.mime))
      .slice(0, MAX_ATTACHMENTS)
      .map((a) => ({ mime: a.mime, data: a.data }));
  }

  if (!trimmedQuestion && !safeAttachments.length) {
    res.status(400).json({ error: "질문을 입력하거나 사진/PDF를 첨부해주세요." });
    return;
  }
  if (trimmedQuestion.length > MAX_QUESTION_LENGTH) {
    res.status(400).json({ error: `질문이 너무 깁니다 (최대 ${MAX_QUESTION_LENGTH}자).` });
    return;
  }
  const totalB64 = safeAttachments.reduce((n, a) => n + a.data.length, 0);
  if (totalB64 > MAX_ATTACH_BASE64_TOTAL) {
    res.status(413).json({ error: "첨부 파일이 너무 커요 (합계 약 2.8MB 이하). 사진은 자동으로 줄여지지만 PDF는 작은 것만 가능해요. 필요한 페이지만 캡처해서 올려보세요." });
    return;
  }

  let safeHistory = [];
  if (Array.isArray(history)) {
    safeHistory = history
      .filter((h) => h && typeof h.text === "string" && (h.role === "user" || h.role === "assistant"))
      .map((h) => ({ role: h.role, text: h.text.slice(0, 4000) }))
      .slice(-MAX_HISTORY_TURNS);
  }

  // 단계 선택: deep 요청이어도 Pro 모델이 설정돼있지 않거나 시간당 한도를 넘으면 fast로 처리한다.
  let tier = "fast";
  let downgradeNote = null;
  if (mode === "deep") {
    if (!DEEP_MODELS.length) downgradeNote = "정밀 모드가 아직 켜져있지 않아 빠른 모드로 답했어요.";
    else if (hit(deepHits, ip, 3_600_000, DEEP_PER_HOUR)) downgradeNote = "정밀 모드 사용 한도(시간당)를 넘어 빠른 모드로 답했어요.";
    else tier = "deep";
  }

  try {
    const deadlineAt = startedAt + TOTAL_TIME_BUDGET_MS;

    // 1) 문제 은행 검색 (실패해도 답변은 계속 진행)
    let hits = [];
    let retrieval = "none";
    try {
      const extracted = await extractQuery(safeAttachments);
      const queryText = [trimmedQuestion, extracted && extracted.problem_text].filter(Boolean).join(" ");
      const bank = await loadBank();
      hits = searchBank(bank, queryText, extracted && extracted.subject);
      if (hits.length) {
        await fetchDetails(hits);
        retrieval = hits[0].score >= SAME_THRESHOLD ? "same" : "similar";
      }
    } catch (err) {
      console.warn("[문제 은행 검색 생략]", err.message);
    }

    // 2) 본 답변
    const contents = safeHistory.map((h) => ({
      role: h.role === "assistant" ? "model" : "user",
      parts: [{ text: h.text }],
    }));
    const referenceText = buildReferenceText(hits);
    const userParts = [
      ...safeAttachments.map((a) => ({ inline_data: { mime_type: a.mime, data: a.data } })),
      ...(referenceText ? [{ text: referenceText }] : []),
      { text: trimmedQuestion || "첨부한 자료의 문제를 풀어주세요." },
    ];
    contents.push({ role: "user", parts: userParts });

    const payload = { system_instruction: { parts: [{ text: SYSTEM_INSTRUCTION }] }, contents };

    const models = tier === "deep" ? [...DEEP_MODELS, ...FAST_MODELS] : FAST_MODELS;
    const { model, data } = await generate(models, payload, deadlineAt);

    const usedDeep = DEEP_MODELS.includes(model);
    const fellBack = tier === "deep" && !usedDeep;

    const answer = textOf(data);
    if (!answer) {
      res.status(502).json({ error: "AI가 답변을 생성하지 못했어요 (안전 필터 등). 질문을 바꿔서 다시 시도해주세요.", retryable: true });
      return;
    }

    console.log(`[답변] tier=${tier} model=${model} retrieval=${retrieval} hits=${hits.length} ms=${Date.now() - startedAt}`);

    res.status(200).json({
      answer,
      tier: usedDeep ? "deep" : "fast",
      fell_back: fellBack,
      note: downgradeNote || (fellBack ? "정밀 모드가 혼잡/한도 초과라 빠른 모드로 답했어요." : null),
      retrieval,
      related: hits.map((h) => ({
        message_id: h.file.id,
        title: h.file.title,
        subject: h.file.subject,
        year: h.file.year,
        number: h.it.n,
        page: h.it.p,
        text: ((h.detail && h.detail.text) || h.it.t).slice(0, 500),
        answer: (h.detail && h.detail.answer) || h.it.a || "",
        similarity: Math.round(h.score * 100),
        same: h.score >= SAME_THRESHOLD,
        uncertain: Boolean(h.it.u),
      })),
    });
  } catch (err) {
    console.error(err);
    res.status(err.statusCode || 500).json({ error: err.message || "알 수 없는 오류가 발생했습니다.", retryable: Boolean(err.retryable) });
  }
};

module.exports.config = { maxDuration: 60 };
