// worker/index.js  (Cloudflare Worker + 정적 자산)
//
// wrangler.jsonc의 run_worker_first: ["/api/*"] 설정 때문에, /api/ 로 시작하는 요청만 이 Worker로
// 오고 나머지(index.html 등)는 정적 자산(sites/)으로 바로 서빙된다.
//
// /api/view : "파일 관리" 탭에서 PDF를 웹사이트 안(<iframe>)에서 바로 볼 수 있게 해주는 프록시.
//
// 왜 프록시가 필요한가
// GitHub Release 자산 URL은 응답에 `Content-Disposition: attachment`가 붙어서 나온다
// (GitHub이 서명해서 리다이렉트하는 URL 자체에 박혀 있음). 그대로 <iframe>에 넣으면
// "보기"가 아니라 다운로드가 시작된다. 또 브라우저 fetch()로 직접 받으려 해도 CORS에 막힌다.
// 그래서 이 함수가 대신 받아서 Content-Disposition을 inline으로 바꿔 중계한다.
//
// Vercel 버전(api/view.js)과 달라진 점
// 1) manifest.json을 읽지 않는다. manifest는 약 7MB(1만 3천 개)라 JSON.parse에만 약 160ms가
//    걸리는데, Workers 무료 플랜은 요청당 CPU 시간이 10ms다. 대신 프론트가 이미 가진
//    download_url을 ?u=로 넘기고, 이 함수는 "이 저장소의 release 주소인가"만 검사한다.
// 2) PDF를 메모리에 올리지 않고 스트리밍으로 흘려보낸다 (Workers 메모리 128MB 한도 때문에도 필수).
// 3) Range 요청(Range: bytes=a-b)을 원본에 그대로 전달하고 206 응답을 중계한다.
//    브라우저 PDF 뷰어는 첫 화면에 필요한 조각만 먼저 받으므로 큰 파일도 빨리 뜬다.
// 4) Cloudflare는 전송량(egress) 요금/한도가 없다 (Vercel처럼 월 100GB에 걸리지 않음).
//    대신 무료 플랜은 이 Worker 호출이 하루 10만 건이 한도다. (정적 파일 요청은 무료/무제한)
//    넘으면 청구되는 게 아니라 그날 리셋될 때까지 /api/* 가 멈춘다. 유료 플랜($5/월)이면 풀린다.
//
// 요청: GET|HEAD /api/view?u=<release download_url (URL 인코딩)>   (선택: Range 헤더)
// 응답: PDF 바이너리 (200 / 206 / 416), 실패 시 JSON { error }

import { handleAsk } from "./ask.js";

// index.html의 GITHUB_OWNER / GITHUB_REPO와 반드시 동일하게 유지한다.
const GITHUB_OWNER = "StockGame-Create";
const GITHUB_REPO = "unvul-files";

// 열린 프록시가 되지 않도록, 이 저장소의 release 다운로드 주소만 허용한다.
// (일반 파일은 .../releases/download/large-files-N/..., 평가원 자료는 .../mock-files/...)
const ALLOWED_HOST = "github.com";
const ALLOWED_PATH_PREFIX = `/${GITHUB_OWNER}/${GITHUB_REPO}/releases/download/`;

function jsonError(status, message) {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

// ?u=가 허용된 release 주소인지 검사하고, 맞으면 URL 객체를 돌려준다.
function parseTarget(request) {
  const raw = new URL(request.url).searchParams.get("u");
  if (!raw) return null;
  let target;
  try {
    target = new URL(raw);
  } catch {
    return null;
  }
  if (target.protocol !== "https:" || target.hostname !== ALLOWED_HOST) return null;
  if (target.username || target.password || target.port) return null;
  // URL 파서가 ../ 같은 경로를 이미 정규화한 뒤의 pathname으로 검사한다.
  if (!target.pathname.startsWith(ALLOWED_PATH_PREFIX)) return null;
  return target;
}

// 한 구간짜리 Range만 원본에 전달한다 (여러 구간 요청은 무시하고 전체로 응답).
function pickRange(request) {
  const h = request.headers.get("Range") || "";
  return /^bytes=\d*-\d*$/.test(h) && h !== "bytes=-" ? h : null;
}

// 파일명은 주소의 마지막 조각에서 가져온다. 한글 등 비-ASCII는 filename*(RFC 5987)로 주고,
// filename은 ASCII 근사치 폴백.
function inlineDisposition(target) {
  let name = "file.pdf";
  try {
    const last = target.pathname.split("/").pop();
    if (last) name = decodeURIComponent(last);
  } catch {
    // 디코딩 실패하면 기본 이름 사용
  }
  const ascii = name.replace(/["\\]/g, "").replace(/[^\x20-\x7E]/g, "_");
  return `inline; filename="${ascii}"; filename*=UTF-8''${encodeURIComponent(name)}`;
}

async function handleView(request) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return jsonError(405, "GET/HEAD만 지원합니다.");
  }

  const target = parseTarget(request);
  if (!target) return jsonError(400, "허용되지 않은 주소입니다.");

  const range = pickRange(request);

  let upstream;
  try {
    upstream = await fetch(target.toString(), {
      method: "GET",
      headers: range ? { Range: range } : {},
      redirect: "follow", // github.com/.../download/... -> 서명된 objects.githubusercontent.com
    });
  } catch (err) {
    return jsonError(502, "원본 PDF를 받아오지 못했습니다.");
  }

  const okStatus = upstream.status === 200 || upstream.status === 206 || upstream.status === 416;
  if (!okStatus) {
    if (upstream.body) upstream.body.cancel();
    return jsonError(upstream.status === 404 ? 404 : 502, "원본 PDF를 받아오지 못했습니다.");
  }

  const headers = new Headers();
  headers.set("Content-Type", "application/pdf");
  headers.set("Content-Disposition", inlineDisposition(target));
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Cache-Control", "public, max-age=3600");
  for (const h of ["Content-Length", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"]) {
    const v = upstream.headers.get(h);
    if (v) headers.set(h, v);
  }
  // 원본이 206을 줬다면 Range를 지원하는 것이므로 뷰어에게 알려준다.
  if (upstream.status === 206 && !headers.has("Accept-Ranges")) headers.set("Accept-Ranges", "bytes");

  if (request.method === "HEAD" || upstream.status === 416) {
    if (upstream.body) upstream.body.cancel();
    return new Response(null, { status: upstream.status, headers });
  }

  // 본문은 메모리에 올리지 않고 그대로 흘려보낸다.
  return new Response(upstream.body, { status: upstream.status, headers });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);
    if (pathname === "/api/view") return handleView(request);
    if (pathname === "/api/ask") {
      if (request.method !== "POST") return jsonError(405, "POST만 지원합니다.");
      return handleAsk(request, env);
    }
    // 이 Worker가 제공하지 않는 API
    return jsonError(404, "이 주소에서 제공하지 않는 기능이에요.");
  },
};
