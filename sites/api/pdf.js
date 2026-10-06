// api/pdf.js
//
// 자체 PDF 뷰어(index.html의 PDF.js)용 엔드포인트. api/view.js와 달리 "일부분만" 줄 수 있다.
//
// view.js의 한계 (이게 이 파일을 만든 이유)
// 1) PDF 전체를 메모리에 올렸다가 한 번에 응답한다 -> 느리고,
//    Vercel 함수의 응답 본문 한도(약 4.5MB, 스트리밍 아닌 경우)에 걸릴 수 있다.
// 2) 사용자가 3쪽만 봐도 파일 전체 크기만큼 Vercel 아웃바운드 대역폭이 나간다.
//
// 이 파일의 방식
// - 브라우저/PDF.js가 보내는 `Range: bytes=a-b` 요청을 GitHub Release(원본)에 그대로 전달하고,
//   받은 일부분(206)을 스트리밍으로 흘려보낸다. PDF.js는 필요한 쪽의 조각만 요청하므로
//   첫 화면이 빨리 뜨고, 대역폭도 "실제로 본 만큼"만 쓴다.
// - 원본 서버가 Range를 무시하고 전체(200)를 주는 경우에도 동작하도록, 필요한 구간만
//   잘라서 내려보내는 폴백이 있다 (이 경우 GitHub -> Vercel 구간은 전체를 읽지만,
//   Vercel -> 사용자 구간은 요청한 구간만 나간다).
// - Content-Disposition: inline 으로 응답한다 (원본은 attachment라 iframe/뷰어에서 다운로드가 됨).
//
// 요청: GET|HEAD /api/pdf?message_id=123   (선택: Range 헤더)
// 응답: PDF 바이너리 (200 또는 206), 실패 시 JSON { error }
//
// 참고: 같은 이유(원본이 GitHub)로 CORS 문제도 없다 - 프론트와 같은 오리진에서 호출한다.

const { Readable } = require("stream");
const { pipeline } = require("stream/promises");

// index.html / view.js / ask.js와 동일하게 유지
const GITHUB_OWNER = "StockGame-Create";
const GITHUB_REPO = "unvul-files";
const GITHUB_BRANCH = "main";
const MANIFEST_URL = `https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/${GITHUB_BRANCH}/sites/manifest.json`;
const RAW_FILES_BASE = `https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/${GITHUB_BRANCH}/sites/files/`;
// 평가원(수능/모평) 자료 manifest (sync2.py). 없거나 실패해도 기본 manifest 조회에는 영향 없다.
const MOCK_MANIFEST_URL = `https://raw.githubusercontent.com/${GITHUB_OWNER}/${GITHUB_REPO}/${GITHUB_BRANCH}/sites/manifest_mock.json`;

// 부분 전송이라 서버 메모리/응답 한도 걱정이 없어서 view.js(100MB)보다 넉넉히 둔다.
// index.html의 MAX_VIEWABLE_BYTES와 동일하게 유지할 것.
const MAX_VIEWABLE_BYTES = 300 * 1024 * 1024;

// PDF.js는 한 화면을 그리는 동안 Range 요청을 여러 번 보내므로, 요청마다 manifest를
// 다시 받지 않게 잠깐 기억해둔다 (서버리스 인스턴스가 살아있는 동안만).
const MANIFEST_TTL_MS = 5 * 60 * 1000;
let manifestCache = { at: 0, byId: null };

async function findFile(messageId) {
  if (!manifestCache.byId || Date.now() - manifestCache.at > MANIFEST_TTL_MS) {
    try {
      const res = await fetch(MANIFEST_URL, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const manifest = await res.json();
      const byId = new Map((manifest.files || []).map((f) => [f.message_id, f]));
      try {
        const mockRes = await fetch(MOCK_MANIFEST_URL, { cache: "no-store" });
        if (mockRes.ok) for (const f of (await mockRes.json()).files || []) byId.set(f.message_id, f);
      } catch (mockErr) {
        console.warn("[manifest_mock 조회 실패, 평가원 자료만 건너뜀]", mockErr.message);
      }
      manifestCache = { at: Date.now(), byId };
    } catch (err) {
      if (!manifestCache.byId) throw new Error("manifest.json을 불러오지 못했습니다.");
      console.warn("[manifest 갱신 실패, 이전 캐시 사용]", err.message);
    }
  }
  return manifestCache.byId.get(messageId) || null;
}

function getMessageId(req) {
  if (req.query && req.query.message_id !== undefined) return Number(req.query.message_id);
  try {
    return Number(new URL(req.url, "http://localhost").searchParams.get("message_id"));
  } catch {
    return NaN;
  }
}

// "bytes=a-b" / "bytes=a-" / "bytes=-n" 한 구간만 지원 (여러 구간은 전체 응답으로 대체).
function parseRange(header, total) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header || "");
  if (!m || (m[1] === "" && m[2] === "")) return null;
  let start, end;
  if (m[1] === "") {
    start = Math.max(0, total - Number(m[2]));
    end = total - 1;
  } else {
    start = Number(m[1]);
    end = m[2] === "" ? total - 1 : Math.min(Number(m[2]), total - 1);
  }
  if (start > end || start >= total) return "invalid";
  return { start, end };
}

// 원본이 전체(200)를 줬을 때 [start, end] 구간만 골라서 내보내는 스트림.
async function* sliceBody(body, start, end) {
  let pos = 0;
  for await (const chunk of body) {
    const next = pos + chunk.length;
    if (next > start && pos <= end) {
      yield chunk.subarray(Math.max(0, start - pos), Math.min(chunk.length, end + 1 - pos));
    }
    pos = next;
    if (pos > end) return; // 필요한 만큼 읽었으면 원본 읽기를 중단
  }
}

function setCommonHeaders(res, file) {
  // 한글 등 비-ASCII 파일명은 filename*(RFC 5987)로, filename은 ASCII 근사치 폴백.
  const rawName = file.filename || "file.pdf";
  const ascii = rawName.replace(/["\\]/g, "").replace(/[^\x20-\x7E]/g, "_");
  res.setHeader("Content-Type", "application/pdf");
  res.setHeader("Content-Disposition", `inline; filename="${ascii}"; filename*=UTF-8''${encodeURIComponent(rawName)}`);
  res.setHeader("Accept-Ranges", "bytes");
  res.setHeader("Cache-Control", "public, max-age=3600");
}

module.exports = async function handler(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.status(405).json({ error: "GET/HEAD만 지원합니다." });
    return;
  }

  const messageId = getMessageId(req);
  if (!Number.isFinite(messageId)) {
    res.status(400).json({ error: "message_id가 올바르지 않습니다." });
    return;
  }

  const ac = new AbortController();
  res.on("close", () => ac.abort()); // 사용자가 중간에 끊으면(PDF.js가 자주 그런다) 원본 읽기도 중단

  try {
    const file = await findFile(messageId);
    if (!file) {
      res.status(404).json({ error: "해당 자료를 찾을 수 없습니다." });
      return;
    }

    const pdfUrl = file.download_url || (file.stored_as ? `${RAW_FILES_BASE}${encodeURIComponent(file.stored_as)}` : null);
    if (!pdfUrl) {
      res.status(404).json({ error: "원본 파일 위치를 찾을 수 없습니다." });
      return;
    }
    if (file.size_bytes && file.size_bytes > MAX_VIEWABLE_BYTES) {
      res.status(413).json({
        error: `이 자료는 ${(file.size_bytes / 1048576).toFixed(0)}MB로 너무 커서 바로 보기는 지원하지 않아요 (현재 한도: ${MAX_VIEWABLE_BYTES / 1048576}MB). 다운로드로 받아서 열어주세요.`,
      });
      return;
    }

    const rangeHeader = typeof req.headers.range === "string" ? req.headers.range : "";
    const wantsRange = /^bytes=\d*-\d*$/.test(rangeHeader) && rangeHeader !== "bytes=-";
    const upstream = await fetch(pdfUrl, {
      method: "GET",
      headers: wantsRange ? { Range: rangeHeader } : {},
      redirect: "follow", // github.com/.../download/... -> 서명된 objects.githubusercontent.com
      signal: ac.signal,
    });

    setCommonHeaders(res, file);

    if (upstream.status === 416) {
      const cr = upstream.headers.get("content-range");
      if (cr) res.setHeader("Content-Range", cr);
      res.status(416).end();
      return;
    }
    if (!upstream.ok) throw new Error(`원본 PDF를 받아오지 못했습니다 (HTTP ${upstream.status}).`);

    // 1) 원본이 Range를 처리해줌: 206을 그대로 중계
    if (upstream.status === 206) {
      res.status(206);
      for (const h of ["content-range", "content-length"]) {
        const v = upstream.headers.get(h);
        if (v) res.setHeader(h, v);
      }
      if (req.method === "HEAD") return res.end();
      await pipeline(Readable.fromWeb(upstream.body), res);
      return;
    }

    // 2) 원본이 전체(200)를 줌
    const total = Number(upstream.headers.get("content-length")) || file.size_bytes || 0;
    const range = wantsRange && total ? parseRange(rangeHeader, total) : null;

    if (range === "invalid") {
      res.setHeader("Content-Range", `bytes */${total}`);
      res.status(416).end();
      return;
    }
    if (range) {
      // Range를 무시하는 원본 -> 필요한 구간만 잘라서 응답
      res.status(206);
      res.setHeader("Content-Range", `bytes ${range.start}-${range.end}/${total}`);
      res.setHeader("Content-Length", String(range.end - range.start + 1));
      if (req.method === "HEAD") return res.end();
      await pipeline(Readable.from(sliceBody(upstream.body, range.start, range.end), { objectMode: false }), res);
      return;
    }

    // Range 없는 일반 요청 (PDF.js도 처음엔 이렇게 요청해서 헤더만 보고 끊고 이후 Range로 요청한다)
    res.status(200);
    if (total) res.setHeader("Content-Length", String(total));
    if (req.method === "HEAD") return res.end();
    await pipeline(Readable.fromWeb(upstream.body), res);
  } catch (err) {
    if (ac.signal.aborted) return; // 클라이언트가 끊은 것 - 오류 아님
    console.error(err);
    if (!res.headersSent) res.status(500).json({ error: err.message || "알 수 없는 오류가 발생했습니다." });
    else res.destroy();
  }
};

// 큰 구간을 천천히 받아가는 느린 연결도 끊기지 않도록 넉넉히 (Hobby 최대 60초).
module.exports.config = { maxDuration: 60 };
