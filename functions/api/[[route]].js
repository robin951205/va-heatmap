// 관리자 API
//  공개(운영·검증): GET health · POST request(관리자 권한요청)
//  검증계 비로그인: POST setup(최초 마스터 지정) · POST login
//  관리자: GET me · POST logout · GET diff · POST promote(이관)
//  마스터: GET admin/requests · POST admin/decide · GET admin/users · POST admin/revoke · POST admin/reset-code
//          POST admin/transfer(마스터 인계) · GET admin/audit · POST admin/delete-request
import { isProd, json, db, sha256, randomHex, newAccessCode, normCode, nowIso, audit, currentAdmin, COOKIE, purgeExpired, RETENTION_YEARS } from "../_shared.js";

const SESSION_H = 12;
const clean = (s, max) => String(s ?? "").replace(/[\u0000-\u001f<>]/g, "").trim().slice(0, max);

export async function onRequest(ctx) {
  const { request, env, params } = ctx;
  const route = (params.route || []).join("/");
  const m = request.method;
  try {
    if (m === "POST") {
      // 다른 사이트에서 보낸 요청 차단 (같은 출처 + JSON 만)
      const origin = request.headers.get("origin");
      if (origin && origin !== new URL(request.url).origin) return json({ error: "허용되지 않은 요청" }, 403);
      if (!(request.headers.get("content-type") || "").includes("application/json")) return json({ error: "JSON 요청만 허용" }, 415);
    }
    const body = m === "POST" ? await request.json().catch(() => ({})) : {};
    const prod = isProd(env, request);

    if (route === "health") return json({ ok: true, stage: prod ? "production" : "staging", db: !!env.DB, retention_years: RETENTION_YEARS });
    if (route === "request" && m === "POST") return await requestAdmin(env, request, body);
    if (prod) return json({ error: "관리 기능은 검증계에서만 사용할 수 있습니다." }, 404);

    if (route === "setup" && m === "POST") return await setup(env, body);
    if (route === "login" && m === "POST") return await login(env, request, body);

    const me = await currentAdmin(env, request);
    if (!me) return json({ error: "로그인 필요" }, 401);
    if (route === "me") return json({ admin: me });
    if (route === "logout" && m === "POST") return await logout(env, request);
    if (route === "diff") return await diff(env);
    if (route === "promote" && m === "POST") return await promote(env, me);

    if (!route.startsWith("admin/")) return json({ error: "없는 경로" }, 404);
    if (me.role !== "master") return json({ error: "마스터 관리자만 사용할 수 있습니다." }, 403);
    const D = await db(env), sub = route.slice(6);
    ctx.waitUntil(purgeExpired(env).catch(() => {}));   // 마스터가 관리 화면을 열 때마다 기한 지난 기록 파기
    if (sub === "requests") {
      const { results } = await D.prepare("SELECT id,empno,name,org,status,note,created_at,decided_at,decided_by FROM requests ORDER BY (status='pending') DESC, id DESC LIMIT 200").all();
      return json({ requests: results });
    }
    if (sub === "users") {
      const { results } = await D.prepare("SELECT empno,name,org,role,status,created_at,approved_by FROM admins ORDER BY (role='master') DESC, status, created_at").all();
      return json({ users: results });
    }
    if (sub === "audit") {
      const { results } = await D.prepare("SELECT at,actor,action,detail FROM audit ORDER BY id DESC LIMIT 100").all();
      return json({ audit: results });
    }
    if (m !== "POST") return json({ error: "없는 경로" }, 404);
    if (sub === "decide") return await decide(env, me, body);
    if (sub === "revoke") return await revoke(env, me, body);
    if (sub === "reset-code") return await resetCode(env, me, body);
    if (sub === "transfer") return await transfer(env, me, body);
    if (sub === "delete-request") {
      await D.prepare("DELETE FROM requests WHERE id=? AND status!='pending'").bind(+body.id).run();
      await audit(env, me.empno, "신청기록 삭제", { id: +body.id });
      return json({ ok: true });
    }
    return json({ error: "없는 경로" }, 404);
  } catch (e) {
    return json({ error: String(e.message || e) }, 500);
  }
}

// ── 권한요청 (운영계 팝업) ──
async function requestAdmin(env, request, b) {
  const org = clean(b.org, 40), empno = clean(b.empno, 20).toUpperCase(), name = clean(b.name, 20);
  if (!org || !name || !/^[0-9A-Z-]{3,20}$/.test(empno)) return json({ error: "소속·사번·이름을 확인해 주세요. (사번은 영문·숫자 3~20자)" }, 400);
  if (!b.agree) return json({ error: "개인정보 수집·이용에 동의해 주세요." }, 400);
  const D = await db(env), ip = await sha256((request.headers.get("cf-connecting-ip") || "") + (env.SETUP_KEY || ""));
  const recent = await D.prepare("SELECT COUNT(*) n FROM requests WHERE ip_hash=? AND created_at>?").bind(ip, new Date(Date.now() - 3600e3).toISOString()).first();
  if (recent.n >= 5) return json({ error: "잠시 후 다시 시도해 주세요." }, 429);
  if (await D.prepare("SELECT 1 FROM admins WHERE empno=? AND status='active'").bind(empno).first()) return json({ error: "이미 관리자로 등록된 사번입니다." }, 409);
  if (await D.prepare("SELECT 1 FROM requests WHERE empno=? AND status='pending'").bind(empno).first()) return json({ error: "이미 접수된 신청이 있습니다. 승인을 기다려 주세요." }, 409);
  await D.prepare("INSERT INTO requests(empno,name,org,status,created_at,ip_hash) VALUES(?,?,?,'pending',?,?)").bind(empno, name, org, nowIso(), ip).run();
  await audit(env, empno, "권한요청 접수", { org, name });
  return json({ ok: true, message: "신청이 접수되었습니다. 마스터 관리자 승인 후 접속코드를 전달받습니다." });
}

// ── 최초 마스터 지정 (마스터가 없을 때 1회, SETUP_KEY 필요) ──
async function setup(env, b) {
  if (!env.SETUP_KEY || b.setup_key !== env.SETUP_KEY) return json({ error: "설정키가 맞지 않습니다." }, 403);
  const D = await db(env);
  if (await D.prepare("SELECT 1 FROM admins WHERE role='master' AND status='active'").first()) return json({ error: "이미 마스터 관리자가 있습니다." }, 409);
  const empno = clean(b.empno, 20).toUpperCase(), name = clean(b.name, 20), org = clean(b.org, 40);
  if (!name || !/^[0-9A-Z-]{3,20}$/.test(empno)) return json({ error: "사번·이름을 확인해 주세요." }, 400);
  const code = await putAdmin(D, { empno, name, org, role: "master", by: "setup" });
  await audit(env, empno, "마스터 지정(최초)", { name });
  return json({ ok: true, code });
}

async function putAdmin(D, { empno, name, org, role, by }) {
  const code = newAccessCode(), salt = randomHex(16);
  await D.prepare(`INSERT INTO admins(empno,name,org,role,status,code_hash,code_salt,created_at,approved_by) VALUES(?,?,?,?,'active',?,?,?,?)
      ON CONFLICT(empno) DO UPDATE SET name=excluded.name, org=excluded.org, role=excluded.role, status='active', revoked_at=NULL,
      code_hash=excluded.code_hash, code_salt=excluded.code_salt, approved_by=excluded.approved_by`)
    .bind(empno, name, org, role, await sha256(salt + normCode(code)), salt, nowIso(), by).run();
  return code;
}

// ── 로그인 (사번 + 접속코드) ──
async function login(env, request, b) {
  const D = await db(env), empno = clean(b.empno, 20).toUpperCase(), ipk = "ip:" + (request.headers.get("cf-connecting-ip") || "");
  const since = Date.now() - 15 * 60e3;
  await D.prepare("DELETE FROM fails WHERE at<?").bind(since).run();
  const f = await D.prepare("SELECT COUNT(*) n FROM fails WHERE k IN (?,?)").bind("emp:" + empno, ipk).first();
  if (f.n >= 8) return json({ error: "로그인 시도가 많습니다. 15분 후 다시 시도해 주세요." }, 429);
  const a = await D.prepare("SELECT * FROM admins WHERE empno=? AND status='active'").bind(empno).first();
  if (!a || (await sha256(a.code_salt + normCode(b.code))) !== a.code_hash) {
    await D.batch([D.prepare("INSERT INTO fails VALUES(?,?)").bind("emp:" + empno, Date.now()), D.prepare("INSERT INTO fails VALUES(?,?)").bind(ipk, Date.now())]);
    return json({ error: "사번 또는 접속코드가 맞지 않습니다." }, 401);
  }
  const tok = randomHex(32);
  await D.prepare("DELETE FROM sessions WHERE expires_at<?").bind(Date.now()).run();
  await D.prepare("INSERT INTO sessions VALUES(?,?,?)").bind(await sha256(tok), empno, Date.now() + SESSION_H * 3600e3).run();
  await audit(env, empno, "로그인", "");
  return json({ ok: true }, 200, { "set-cookie": `${COOKIE}=${tok}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_H * 3600}` });
}

async function logout(env, request) {
  const tok = (request.headers.get("cookie") || "").match(new RegExp(`${COOKIE}=([^;]+)`))?.[1];
  if (tok) await (await db(env)).prepare("DELETE FROM sessions WHERE token_hash=?").bind(await sha256(tok)).run();
  return json({ ok: true }, 200, { "set-cookie": `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0` });
}

// ── 승인·반려 ──
async function decide(env, me, b) {
  const D = await db(env), r = await D.prepare("SELECT * FROM requests WHERE id=? AND status='pending'").bind(+b.id).first();
  if (!r) return json({ error: "처리할 신청이 없습니다." }, 404);
  const note = clean(b.note, 200);
  if (b.approve) {
    const code = await putAdmin(D, { empno: r.empno, name: r.name, org: r.org, role: "admin", by: me.empno });
    await D.prepare("UPDATE requests SET status='approved', note=?, decided_at=?, decided_by=? WHERE id=?").bind(note, nowIso(), me.empno, r.id).run();
    await audit(env, me.empno, "권한요청 승인", { empno: r.empno, name: r.name });
    return json({ ok: true, code, message: `${r.name}(${r.empno}) 승인. 접속코드를 본인에게 직접 전달하세요. 이 화면을 닫으면 다시 볼 수 없습니다.` });
  }
  await D.prepare("UPDATE requests SET status='rejected', note=?, decided_at=?, decided_by=? WHERE id=?").bind(note, nowIso(), me.empno, r.id).run();
  await audit(env, me.empno, "권한요청 반려", { empno: r.empno, name: r.name, note });
  return json({ ok: true, message: "반려 처리했습니다." });
}

async function revoke(env, me, b) {
  const empno = clean(b.empno, 20).toUpperCase();
  if (empno === me.empno) return json({ error: "본인 권한은 해제할 수 없습니다. 먼저 마스터를 인계하세요." }, 400);
  const D = await db(env);
  await D.batch([D.prepare("UPDATE admins SET status='revoked', revoked_at=? WHERE empno=? AND role!='master'").bind(nowIso(), empno), D.prepare("DELETE FROM sessions WHERE empno=?").bind(empno)]);
  await audit(env, me.empno, "관리자 해제", { empno });
  return json({ ok: true });
}

async function resetCode(env, me, b) {
  const D = await db(env), empno = clean(b.empno, 20).toUpperCase();
  const a = await D.prepare("SELECT * FROM admins WHERE empno=? AND status='active'").bind(empno).first();
  if (!a) return json({ error: "활성 관리자가 아닙니다." }, 404);
  const code = await putAdmin(D, { empno, name: a.name, org: a.org, role: a.role, by: a.approved_by });
  await D.prepare("DELETE FROM sessions WHERE empno=?").bind(empno).run();
  await audit(env, me.empno, "접속코드 재발급", { empno });
  return json({ ok: true, code });
}

// ── 마스터 인계: 본인 접속코드 재확인 후, 대상(활성 일반관리자)을 마스터로, 본인은 일반관리자로 ──
async function transfer(env, me, b) {
  const D = await db(env), to = clean(b.empno, 20).toUpperCase();
  const self = await D.prepare("SELECT * FROM admins WHERE empno=?").bind(me.empno).first();
  if ((await sha256(self.code_salt + normCode(b.code))) !== self.code_hash) return json({ error: "본인 접속코드가 맞지 않습니다." }, 401);
  const t = await D.prepare("SELECT * FROM admins WHERE empno=? AND status='active' AND role='admin'").bind(to).first();
  if (!t) return json({ error: "인계 대상은 활성 일반관리자여야 합니다." }, 400);
  await D.batch([D.prepare("UPDATE admins SET role='master' WHERE empno=?").bind(to), D.prepare("UPDATE admins SET role='admin' WHERE empno=?").bind(me.empno)]);
  await audit(env, me.empno, "마스터 인계", { to, name: t.name });
  return json({ ok: true, message: `${t.name}(${to})에게 마스터 관리자를 인계했습니다. 본인은 일반관리자가 됩니다.` });
}

// ── 이관: GitHub 에서 develop → main 병합 (운영계는 main 기준 자동 배포) ──
async function gh(env, path, init = {}) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) throw new Error("GITHUB_TOKEN·GITHUB_REPO 설정이 없습니다.");
  return fetch(`https://api.github.com/repos/${env.GITHUB_REPO}${path}`, {
    ...init, headers: { authorization: `Bearer ${env.GITHUB_TOKEN}`, accept: "application/vnd.github+json", "user-agent": "va-heatmap-admin", ...(init.headers || {}) },
  });
}

async function diff(env) {
  const r = await gh(env, "/compare/main...develop");
  if (!r.ok) return json({ error: `GitHub ${r.status}` }, 502);
  const d = await r.json();
  // 일 배치 데이터 커밋(main)은 이관 대상이 아님 → develop 쪽 커밋만
  const commits = (d.commits || []).filter(c => !/^Merge branch 'main'/.test(c.commit.message)).map(c => ({
    sha: c.sha.slice(0, 7), msg: c.commit.message.split("\n")[0],
    date: new Date(c.commit.author.date).toLocaleString("sv-SE", { timeZone: "Asia/Seoul" }).slice(0, 16),
  })).reverse();
  return json({ ahead: commits.length, behind: d.behind_by, commits });
}

async function promote(env, me) {
  const d = await (await diff(env)).json();
  if (d.error) return json(d, 502);
  if (!d.ahead) return json({ message: "이관할 변경이 없습니다." });
  const r = await gh(env, "/merges", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ base: "main", head: "develop", commit_message: `이관: 검증계 → 운영계 (${me.name}, ${d.ahead}건)\n\n` + d.commits.map(c => `- ${c.msg}`).join("\n") }),
  });
  if (r.status === 201 || r.status === 204) {
    await audit(env, me.empno, "이관", { count: d.ahead, commits: d.commits.map(c => c.sha) });
    return json({ ok: true, message: `이관 완료 (${d.ahead}건). 1~2분 뒤 운영계에 반영됩니다.` });
  }
  const t = await r.text();
  await audit(env, me.empno, "이관 실패", { status: r.status, body: t.slice(0, 200) });
  return json({ error: r.status === 409 ? "운영계와 충돌이 있어 자동 이관할 수 없습니다. 개발 담당자에게 요청하세요." : `이관 실패 (GitHub ${r.status})` }, 502);
}
