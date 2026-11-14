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
    // 해제일 컬럼(보관기간 계산용). 이미 있으면 오류 → 무시
    for (const c of ["revoked_at TEXT", "pw_hash TEXT", "pw_salt TEXT", "pw_iter INTEGER", "must_change INTEGER DEFAULT 1"])
      try { await env.DB.prepare(`ALTER TABLE admins ADD COLUMN ${c}`).run(); } catch (_) {}   // 이미 있으면 무시
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
  const row = await D.prepare(`SELECT a.empno,a.name,a.org,a.role,COALESCE(a.must_change,1) AS must_change FROM sessions s JOIN admins a ON a.empno=s.empno
      WHERE s.token_hash=? AND s.expires_at>? AND a.status='active'`).bind(await sha256(tok), Date.now()).first();
  return row || null;
}

/** 개인정보 보관기간 5년(2026-11-14 결정): 기준일로부터 5년 지난 기록 파기
 *  - 권한요청: 처리일(승인·반려) 기준 / 처리 전 신청은 보관
 *  - 관리자: 권한 해제일 기준 (활성 관리자는 보관)
 *  - 처리 이력(audit): 기록일 기준 */
export const RETENTION_YEARS = 5;
export async function purgeExpired(env) {
  const D = await db(env), cut = new Date(Date.now() - RETENTION_YEARS * 365.25 * 86400e3).toISOString();
  const r = await D.batch([
    D.prepare("DELETE FROM requests WHERE status!='pending' AND decided_at<?").bind(cut),
    D.prepare("DELETE FROM admins WHERE status='revoked' AND revoked_at IS NOT NULL AND revoked_at<?").bind(cut),
    D.prepare("DELETE FROM audit WHERE at<?").bind(cut),
  ]);
  const n = r.reduce((a, x) => a + (x.meta?.changes || 0), 0);
  if (n) await audit(env, "system", "보관기간 경과 파기", { rows: n, before: cut.slice(0, 10) });
  return n;
}

/** 비밀번호: PBKDF2-SHA256 (Workers CPU 한도 고려해 반복 횟수는 사용자별로 저장 → 나중에 올릴 수 있음) */
export const PW_ITER = 20000;
export async function pwHash(pw, salt, iter = PW_ITER) {
  const key = await crypto.subtle.importKey("raw", enc.encode(pw), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt: enc.encode(salt), iterations: iter }, key, 256);
  return [...new Uint8Array(bits)].map(x => x.toString(16).padStart(2, "0")).join("");
}
/** 새 비밀번호 규칙: 10자 이상, 영문·숫자 포함, 사번과 다름 */
export function pwRuleError(pw, empno) {
  if (typeof pw !== "string" || pw.length < 10 || pw.length > 64) return "비밀번호는 10자 이상 64자 이하로 정해 주세요.";
  if (!/[A-Za-z]/.test(pw) || !/[0-9]/.test(pw)) return "비밀번호에 영문과 숫자를 모두 넣어 주세요.";
  if (pw.toUpperCase().includes(String(empno).toUpperCase())) return "비밀번호에 사번을 넣을 수 없습니다.";
  return null;
}
/** 비밀번호 또는 (비밀번호 설정 전이면) 임시 접속코드 확인 → "pw" | "code" | null */
export async function checkSecret(a, secret) {
  if (!a || !secret) return null;
  if (a.pw_hash && (await pwHash(secret, a.pw_salt, a.pw_iter || PW_ITER)) === a.pw_hash) return "pw";
  if (a.code_hash && (a.must_change ?? 1) && (await sha256(a.code_salt + normCode(secret))) === a.code_hash) return "code";
  return null;
}
