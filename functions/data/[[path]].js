// 검증계는 데이터 파일을 운영계에서 그대로 가져와 보여줌 (일 배치는 main 에만 커밋되므로)
import { isProd } from "../_shared.js";

export async function onRequest({ request, env, next }) {
  if (isProd(env, request) || !env.PROD_HOST) return next();
  const u = new URL(request.url);
  let r;
  try { r = await fetch(`https://${env.PROD_HOST}${u.pathname}${u.search}`, { cf: { cacheTtl: 60 } }); }
  catch { return next(); }   // 운영계에 닿지 않으면 검증계 자체 파일 사용
  if (!r.ok) return next();
  const out = new Response(r.body, r);
  out.headers.set("cache-control", "no-store");
  return out;
}
