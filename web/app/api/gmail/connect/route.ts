// Injected by the tenancy work: the Python API now requires a bearer token on every
// route. This proxy runs server-side, so the token stays out of the browser — it must
// NEVER be added to a Response's headers, only to outbound fetch() calls.
function authHeaders(): Record<string, string> {
  const t = process.env.OPENPOKE_API_TOKEN;
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export const runtime = 'nodejs';

export async function POST(req: Request) {
  let body: any = {};
  try {
    body = await req.json();
  } catch {}
  const userId = body?.userId || '';
  const authConfigId = body?.authConfigId || '';

  const serverBase = process.env.PY_SERVER_URL || 'http://localhost:8001';
  const url = `${serverBase.replace(/\/$/, '')}/api/v1/gmail/connect`;

  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', ...authHeaders() },
      body: JSON.stringify({ user_id: userId, auth_config_id: authConfigId }),
    });
    const data = await resp.json().catch(() => ({}));
    return new Response(JSON.stringify(data), {
      status: resp.status,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  } catch (e: any) {
    return new Response(
      JSON.stringify({ ok: false, error: 'Upstream error', detail: e?.message || String(e) }),
      { status: 502, headers: { 'Content-Type': 'application/json; charset=utf-8' } }
    );
  }
}
