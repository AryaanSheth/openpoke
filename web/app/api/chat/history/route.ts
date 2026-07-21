// Injected by the tenancy work: the Python API now requires a bearer token on every
// route. This proxy runs server-side, so the token stays out of the browser — it must
// NEVER be added to a Response's headers, only to outbound fetch() calls.
function authHeaders(): Record<string, string> {
  const t = process.env.OPENPOKE_API_TOKEN;
  return t ? { Authorization: `Bearer ${t}` } : {};
}

const serverBase = process.env.PY_SERVER_URL || 'http://localhost:8001';
const historyPath = `${serverBase.replace(/\/$/, '')}/api/v1/chat/history`;

async function forward(method: 'GET' | 'DELETE') {
  try {
    const res = await fetch(historyPath, {
      method,
      headers: { Accept: 'application/json', ...authHeaders() },
      cache: 'no-store',
    });

    const bodyText = await res.text();
    const headers = new Headers({ 'Content-Type': 'application/json; charset=utf-8' });
    return new Response(bodyText || '{}', { status: res.status, headers });
  } catch (error: any) {
    const message = error?.message || 'Failed to reach Python server';
    return new Response(JSON.stringify({ error: message }), {
      status: 502,
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
    });
  }
}

export async function GET() {
  return forward('GET');
}

export async function DELETE() {
  return forward('DELETE');
}
