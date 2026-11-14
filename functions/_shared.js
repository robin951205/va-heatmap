// 공통: 운영/검증 구분, D1 테이블 준비, 세션 확인
// (_ 로 시작하는 파일은 경로로 노출되지 않도록 _routes.json 에서 제외)

export const COOKIE = "vah_sess";

/** 운영계 = 요청 호스트가 PROD_HOST 와 같을 때만. 설정이 없거나 다르면 검증계로 보고 잠금(안전 쪽 기본값) */
export function isProd(env, request) {
  const host = new URL(request.url).hostname;
  return !!env.PROD_HOST && host === env.PROD_HOST;
}

export const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers } });

const enc = new TextEncoder();
export async function sha256(s) {
  const b = await crypto.subtle.digest("SHA-256", enc.encode(s));
  return [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, "0")).join("");
}
export function randomHex(n = 32) {
  const a = new Uint8Array(n); crypto.getRandomValues(a);
  return [...a].map(x => x.toString(16).padStart(2, "0")).join("");
}
/** 사람이 옮겨 적기 쉬운 접속코드 (헷갈리는 0/O/1/I 제외, 약 60비트) */
export function newAccessCode() {
  const A = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ", a = new Uint8Array(12); crypto.getRandomValues(a);
  const s = [...a].map(x => A[x % 32]).join("");
  return `${s.slice(0, 4)}-${s.slice(4, 8)}-${s.slice(8)}`;
}
export const normCode = c => String(c || "").toUpperCase().replace(/[^0-9A-Z]/g, "");

let ready = false;
export async function db(env) {
  if (!env.DB) throw new Error("D1 바인딩(DB)이 없습니다. Cloudflare Pages 설정 → 바인딩에서 DB 를 연결하세요.");
  if (!ready) {
    await env.DB.batch([
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS admins(empno TEXT PRIMARY KEY, name TEXT, org TEXT, role TEXT, status TEXT,
        code_hash TEXT, code_salt TEXT, created_at TEXT, approved_by TEXT)`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT, empno TEXT, name TEXT, org TEXT,
        status TEXT, note TEXT, created_at TEXT, decided_at TEXT, decided_by TEXT, ip_hash TEXT)`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, empno TEXT, expires_at INTEGER)`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, actor TEXT, action TEXT, detail TEXT)`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS fails(k TEXT, at INTEGER)`),
    ]);
    ready = true;
  }
  return env.DB;
}

export const nowIso = () => new Date().toISOString();

export async function audit(env, actor, action, detail) {
  await (await db(env)).prepare("INSERT INTO audit(at,actor,action,detail) VALUES(?,?,?,?)")
    .bind(nowIso(), actor, action, typeof detail === "string" ? detail : JSON.stringify(detail)).run();
}

function cookieVal(request, name) {
  const m = (request.headers.get("cookie") || "").match(new RegExp(`(?:^|; )${name}=([^;]+)`));
  return m ? m[1] : null;
}

/** 로그인한 관리자 { empno, name, org, role } 또는 null */
export async function currentAdmin(env, request) {
  const tok = cookieVal(request, COOKIE);
  if (!tok || !env.DB) return null;
  const D = await db(env);
  const row = await D.prepare(`SELECT a.empno,a.name,a.org,a.role FROM sessions s JOIN admins a ON a.empno=s.empno
      WHERE s.token_hash=? AND s.expires_at>? AND a.status='active'`).bind(await sha256(tok), Date.now()).first();
  return row || null;
}
