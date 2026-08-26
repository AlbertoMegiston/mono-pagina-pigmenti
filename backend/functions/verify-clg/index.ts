// Edge function: verifica un codice CLG contro la lista caricata dal brand.
// POST { code: "558420726815", context?: {...} }
// -> { outcome: "genuine" | "suspicious" | "fake" | "not_found" | "invalid" }
import { createClient } from "npm:@supabase/supabase-js@2";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const DUP_LIMIT = 10;           // verifiche...
const DUP_WINDOW_DAYS = 30;     // ...in questa finestra => suspicious

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: CORS });
  if (req.method !== "POST") {
    return json({ error: "method not allowed" }, 405);
  }
  let code = "";
  let context: unknown = null;
  try {
    const body = await req.json();
    code = String(body.code ?? "").replace(/\D+/g, "");
    context = body.context ?? null;
  } catch (_) { /* body vuoto */ }

  if (!/^\d{12}$/.test(code)) return json({ outcome: "invalid" });

  const db = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const { data: row } = await db.from("clg_codes")
    .select("status").eq("code", code).maybeSingle();

  let outcome: string;
  if (!row) outcome = "not_found";
  else if (row.status === "revoked") outcome = "fake";
  else if (row.status === "suspicious") outcome = "suspicious";
  else {
    const since = new Date(Date.now() - DUP_WINDOW_DAYS * 864e5).toISOString();
    const { count } = await db.from("clg_checks")
      .select("id", { count: "exact", head: true })
      .eq("code", code).gte("checked_at", since);
    outcome = (count ?? 0) >= DUP_LIMIT ? "suspicious" : "genuine";
  }

  await db.from("clg_checks").insert({ code, outcome, context });
  return json({ outcome });
});

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}
