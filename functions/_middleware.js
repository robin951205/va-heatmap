// 운영계: 공개. 관리 화면(/admin)은 숨김
// 검증계: 로그인한 관리자만 접근. 첫 화면에 '검증계' 띠(변경 내역·이관·관리) 삽입
import { isProd, currentAdmin } from "./_shared.js";

// Pages 는 .html 을 뗀 주소로 바꿔 보내므로 둘 다 허용
const OPEN_ON_STAGING = ["/admin/login", "/admin/setup", "/admin/admin.css", "/api/login", "/api/setup", "/api/health"];

export async function onRequest(ctx) {
  const { request, env, next } = ctx;
  const path = new URL(request.url).pathname;

  if (isProd(env, request)) {
    if (path.startsWith("/admin")) return new Response("Not found", { status: 404 });
    return next();
  }

  // ── 검증계 ──
  if (OPEN_ON_STAGING.includes(path.replace(/\.html$/, ""))) return next();
  const me = await currentAdmin(env, request);
  if (!me) {
    if (path.startsWith("/api/") || path.startsWith("/data/")) return new Response("로그인 필요", { status: 401 });
    return Response.redirect(new URL(`/admin/login?next=${encodeURIComponent(path)}`, request.url).toString(), 302);
  }
  // 임시 접속코드로 들어온 경우: 본인 비밀번호를 정하기 전에는 비밀번호 화면만
  if (me.must_change && !["/admin/password", "/admin/admin.css", "/api/password", "/api/logout", "/api/me"].includes(path.replace(/\.html$/, ""))) {
    if (path.startsWith("/api/") || path.startsWith("/data/")) return new Response("비밀번호 설정 필요", { status: 403 });
    return Response.redirect(new URL("/admin/password", request.url).toString(), 302);
  }
  const res = await next();
  const type = res.headers.get("content-type") || "";
  if (!(path === "/" || path === "/index.html") || !type.includes("text/html")) {
    const r = new Response(res.body, res); r.headers.set("x-robots-tag", "noindex"); r.headers.set("cache-control", "no-store");
    return r;
  }
  return new HTMLRewriter().on("body", { element(el) { el.prepend(BANNER, { html: true }); } })
    .transform(new Response(res.body, { status: res.status, headers: { "content-type": type, "cache-control": "no-store", "x-robots-tag": "noindex" } }));
}

const BANNER = `
<div id="stgBar" style="background:#7A3E00;color:#fff;font:13px/1.4 Pretendard,system-ui,sans-serif;padding:8px 14px;display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center">
  <b style="background:#fff;color:#7A3E00;border-radius:4px;padding:1px 7px">검증계</b>
  <span id="stgInfo">운영계 대비 변경 확인 중…</span>
  <span style="flex:1"></span>
  <span id="stgMe" style="opacity:.85"></span>
  <a href="/admin/" style="color:#fff">관리</a>
  <button id="stgPromote" disabled style="background:#fff;color:#7A3E00;border:0;border-radius:6px;padding:5px 12px;font-weight:700;cursor:pointer">이관</button>
  <a href="#" id="stgOut" style="color:#fff;opacity:.85">로그아웃</a>
</div>
<style>#stgPromote:disabled{opacity:.45;cursor:not-allowed}</style>
<div id="stgList" style="display:none;background:#FFF4E5;color:#4A2A00;font:12.5px/1.6 system-ui,sans-serif;padding:8px 14px;border-bottom:1px solid #F0D2A8"></div>
<script>
(function(){
  const $=id=>document.getElementById(id), esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  fetch("/api/me").then(r=>r.json()).then(m=>{ if(m.admin) $("stgMe").textContent=m.admin.name+" ("+(m.admin.role==="master"?"마스터 관리자":"관리자")+")"; });
  function load(){
    fetch("/api/diff").then(r=>r.json()).then(d=>{
      if(d.error){ $("stgInfo").textContent="변경 내역 조회 실패: "+d.error; return; }
      $("stgInfo").innerHTML = d.ahead ? "운영계 미반영 변경 <b>"+d.ahead+"건</b> · <a href='#' id='stgShow' style='color:#fff'>내역 보기</a>" : "운영계와 동일 (이관할 변경 없음)";
      $("stgPromote").disabled = !d.ahead;
      $("stgList").innerHTML = d.commits.map(c=>"· "+esc(c.date)+" "+esc(c.msg)).join("<br>");
      const s=$("stgShow"); if(s) s.onclick=e=>{e.preventDefault(); const L=$("stgList"); L.style.display=L.style.display==="none"?"block":"none";};
    }).catch(()=>{ $("stgInfo").textContent="변경 내역 조회 실패"; });
  }
  load();
  $("stgPromote").onclick=async()=>{
    if(!confirm("검증계의 변경 내역을 운영계로 이관합니다.\\n이관 후 약 1~2분 뒤 운영 사이트에 반영됩니다. 진행할까요?")) return;
    $("stgPromote").disabled=true; $("stgInfo").textContent="이관 중…";
    const r=await fetch("/api/promote",{method:"POST",headers:{"content-type":"application/json"},body:"{}"}); const d=await r.json();
    alert(d.message||d.error||"완료"); load();
  };
  $("stgOut").onclick=async e=>{ e.preventDefault(); await fetch("/api/logout",{method:"POST",headers:{"content-type":"application/json"},body:"{}"}); location.href="/admin/login"; };
})();
</script>`;
