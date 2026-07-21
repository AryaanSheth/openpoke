// Injected by the tenancy work: the Python API now requires a bearer token on every
// route. This proxy runs server-side, so the token stays out of the browser — it must
// NEVER be added to a Response's headers, only to outbound fetch() calls.
function authHeaders(): Record<string, string> {
  const t = process.env.OPENPOKE_API_TOKEN;
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export const runtime = 'nodejs';

export async function POST(req: Request) {
  let body: any;
  try {
    body = await req.json();
  } catch (e) {
    return new Response('Invalid JSON', { status: 400 });
  }

  const { timezone } = body || {};
  if (!timezone || typeof timezone !== 'string') {
    return new Response('Missing or invalid timezone', { status: 400 });
  }

  const serverBase = process.env.PY_SERVER_URL || 'http://localhost:8001';
  const url = `${serverBase.replace(/\/$/, '')}/api/v1/meta/timezone`;

  try {
    const upstream = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ timezone }),
    });
    
    if (!upstream.ok) {
      const text = await upstream.text();
      return new Response(text || 'Failed to set timezone', { status: upstream.status });
    }

    const data = await upstream.json();
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  } catch (e: any) {
    return new Response(e?.message || 'Server error', { status: 502 });
  }
}
