// Meeting Hub: посредник к GigaChat (Supabase Edge Function).
// Ключ Сбера хранится только здесь, в секретах проекта, и никогда не попадает в программу и в GitHub.
// Программа ходит сюда с личным кодом доступа сотрудника; код можно отозвать, не меняя ключ Сбера.
//
// Секреты (Edge Functions → Secrets):
//   GIGACHAT_AUTH_KEY — ключ авторизации из кабинета Сбера (в base64, как выдан)
//   GIGACHAT_SCOPE    — необязательно, по умолчанию GIGACHAT_API_PERS
//   SBER_CA_PEM       — корневой сертификат Минцифры (текст .crt целиком), нужен для TLS к серверам Сбера
//   TEAM_TOKENS       — коды доступа через запятую: «имя:код,имя2:код2»
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth";
const CHAT = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions";
const MODELS = new Set(["GigaChat-2", "GigaChat-2-Pro", "GigaChat-2-Max"]);
const MAX_BODY = 400_000;           // байт на запрос
const PER_HOUR = 200;               // запросов в час на один код доступа
const RETRY_MS = [2000, 5000, 10000];

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "content-type, x-hub-token",
};
const json = (obj: unknown, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { ...cors, "Content-Type": "application/json" } });

// deno-lint-ignore no-explicit-any
let client: any;
function tlsClient() {
  const pem = Deno.env.get("SBER_CA_PEM");
  if (!pem) return undefined;
  // deno-lint-ignore no-explicit-any
  client ??= (Deno as any).createHttpClient({ caCerts: [pem] });
  return client;
}
// deno-lint-ignore no-explicit-any
const sber = (url: string, init: any) => fetch(url, { ...init, client: tlsClient() });

let token = "", tokenUntil = 0;
async function accessToken(): Promise<string> {
  if (token && Date.now() < tokenUntil - 60_000) return token;
  const key = (Deno.env.get("GIGACHAT_AUTH_KEY") ?? "").replace(/^Basic\s+/i, "").trim();
  if (!key) throw new Error("GIGACHAT_AUTH_KEY не задан");
  const r = await sber(OAUTH, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
      "RqUID": crypto.randomUUID(), "Authorization": "Basic " + key,
    },
    body: new URLSearchParams({ scope: Deno.env.get("GIGACHAT_SCOPE") || "GIGACHAT_API_PERS" }),
  });
  if (!r.ok) throw new Error(`Сбер не выдал токен: ${r.status} ${(await r.text()).slice(0, 200)}`);
  const d = await r.json();
  token = d.access_token;
  tokenUntil = Number(d.expires_at) > 1e11 ? Number(d.expires_at) : Date.now() + 25 * 60_000;
  return token;
}

function who(req: Request): string | null {
  const given = req.headers.get("x-hub-token") ?? "";
  if (!given) return null;
  for (const item of (Deno.env.get("TEAM_TOKENS") ?? "").split(",")) {
    const i = item.indexOf(":");
    const name = i > 0 ? item.slice(0, i).trim() : item.trim();
    const code = (i > 0 ? item.slice(i + 1) : item).trim();
    if (code.length >= 12 && given.length === code.length) {
      let diff = 0;
      for (let k = 0; k < code.length; k++) diff |= given.charCodeAt(k) ^ code.charCodeAt(k);
      if (diff === 0) return name;
    }
  }
  return null;
}

const hits = new Map<string, number[]>();
function limited(name: string): boolean {
  const now = Date.now(), list = (hits.get(name) ?? []).filter((t) => now - t < 3_600_000);
  list.push(now);
  hits.set(name, list);
  return list.length > PER_HOUR;
}

// Один запрос к Сберу за раз на экземпляр функции: у личного тарифа один поток.
let chain: Promise<unknown> = Promise.resolve();
function queued<T>(job: () => Promise<T>): Promise<T> {
  const run = chain.then(job, job);
  chain = run.catch(() => {});
  return run;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { headers: cors });
  const name = who(req);
  if (!name) return json({ error: "неверный код доступа" }, 401);
  if (limited(name)) return json({ error: "слишком много запросов, попробуйте позже" }, 429);

  if (req.method === "GET") {           // проверка из настроек программы
    try {
      await accessToken();
      return json({ ok: true, user: name });
    } catch (e) {
      return json({ ok: false, user: name, error: String(e instanceof Error ? e.message : e) }, 502);
    }
  }
  if (req.method !== "POST") return json({ error: "метод не поддерживается" }, 405);

  const raw = await req.text();
  if (raw.length > MAX_BODY) return json({ error: "запрос слишком большой" }, 413);
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    return json({ error: "ожидается JSON" }, 400);
  }
  if (!MODELS.has(body.model) || !Array.isArray(body.messages)) return json({ error: "неверные параметры" }, 400);
  const payload = JSON.stringify({
    model: body.model, messages: body.messages, stream: !!body.stream,
    temperature: typeof body.temperature === "number" ? body.temperature : 0.2,
  });

  try {
    const upstream = await queued(async () => {
      let last: Response | undefined;
      for (let attempt = 0; attempt <= RETRY_MS.length; attempt++) {
        last = await sber(CHAT, {
          method: "POST",
          headers: {
            "Authorization": "Bearer " + await accessToken(), "Content-Type": "application/json",
            "Accept": body.stream ? "text/event-stream" : "application/json",
          },
          body: payload,
        });
        if (last.status === 401) token = "";
        if (last.status !== 429 && last.status !== 503) break;
        if (attempt < RETRY_MS.length) {
          await last.body?.cancel();
          await new Promise((r) => setTimeout(r, RETRY_MS[attempt]));
        }
      }
      return last!;
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { ...cors, "Content-Type": upstream.headers.get("Content-Type") ?? "application/json" },
    });
  } catch (e) {
    return json({ error: String(e instanceof Error ? e.message : e) }, 502);
  }
});
