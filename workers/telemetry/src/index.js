/**
 * VentureBot GUIDE_ACCESS Edge Telemetry Worker
 *
 * Implements:
 * 1. POST /event/guide_access: Unauthenticated, non-blocking beacon ingestion.
 *    - Validates experiment UUID and event_type == 'guide_access'.
 *    - Rejects payloads > 1024 bytes.
 *    - Records UTC date bucket on server.
 *    - Atomically increments D1 guide_access_daily counter.
 *    - Persists zero PII, zero IP, zero User-Agent.
 *    - Emits CORS headers for https://vishnu3568.github.io.
 *    - Returns HTTP 204 on success.
 *
 * 2. GET /api/v1/telemetry/summary: Authenticated telemetry retrieval.
 *    - Requires Authorization: Bearer <TELEMETRY_RETRIEVAL_API_KEY>.
 *    - Queries D1 aggregate counts for (experiment_id, date_start, date_stop).
 *    - Returns canonical GuideAccessTelemetrySummary JSON.
 */

const ALLOWED_ORIGIN = "https://vishnu3568.github.io";
const UUID_REGEX = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const DATE_REGEX = /^\d{4}-\d{2}-\d{2}$/;
const MAX_BODY_BYTES = 1024;

function corsHeaders(request) {
  const origin = request.headers.get("Origin");
  // Set CORS origin to allowed origin if matched, or the specified domain
  const allowOrigin = origin === ALLOWED_ORIGIN ? ALLOWED_ORIGIN : ALLOWED_ORIGIN;
  return {
    "Access-Control-Allow-Origin": allowOrigin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // ── 1. Beacon Ingestion: /event/guide_access ────────────────────────────
    if (path === "/event/guide_access") {
      // Handle CORS preflight
      if (request.method === "OPTIONS") {
        return new Response(null, {
          status: 204,
          headers: corsHeaders(request),
        });
      }

      if (request.method !== "POST") {
        return new Response(JSON.stringify({ error: "Method not allowed" }), {
          status: 405,
          headers: {
            "Content-Type": "application/json",
            Allow: "POST, OPTIONS",
            ...corsHeaders(request),
          },
        });
      }

      // Check Content-Length if present
      const contentLength = request.headers.get("content-length");
      if (contentLength && parseInt(contentLength, 10) > MAX_BODY_BYTES) {
        return new Response(JSON.stringify({ error: "Payload too large" }), {
          status: 413,
          headers: { "Content-Type": "application/json", ...corsHeaders(request) },
        });
      }

      // Read text body and enforce byte limit
      let rawBody = "";
      try {
        rawBody = await request.text();
      } catch (err) {
        return new Response(JSON.stringify({ error: "Failed to read request body" }), {
          status: 400,
          headers: { "Content-Type": "application/json", ...corsHeaders(request) },
        });
      }

      if (new TextEncoder().encode(rawBody).length > MAX_BODY_BYTES) {
        return new Response(JSON.stringify({ error: "Payload too large" }), {
          status: 413,
          headers: { "Content-Type": "application/json", ...corsHeaders(request) },
        });
      }

      let payload;
      try {
        payload = JSON.parse(rawBody);
      } catch (err) {
        return new Response(JSON.stringify({ error: "Invalid JSON payload" }), {
          status: 400,
          headers: { "Content-Type": "application/json", ...corsHeaders(request) },
        });
      }

      const experimentId = String(payload.experiment_id || "").trim();
      const eventType = String(payload.event_type || "").trim();

      if (!UUID_REGEX.test(experimentId)) {
        return new Response(JSON.stringify({ error: "Invalid experiment_id format" }), {
          status: 400,
          headers: { "Content-Type": "application/json", ...corsHeaders(request) },
        });
      }

      if (eventType !== "guide_access") {
        return new Response(JSON.stringify({ error: "Unsupported event_type. Only 'guide_access' accepted." }), {
          status: 400,
          headers: { "Content-Type": "application/json", ...corsHeaders(request) },
        });
      }

      // Server-side UTC date bucket (YYYY-MM-DD)
      const now = new Date();
      const dateBucket = now.toISOString().slice(0, 10);

      try {
        const sql = `
          INSERT INTO guide_access_daily (experiment_id, event_type, date_bucket, access_count, created_at, updated_at)
          VALUES (?1, 'guide_access', ?2, 1, datetime('now'), datetime('now'))
          ON CONFLICT(experiment_id, event_type, date_bucket) DO UPDATE SET
            access_count = access_count + 1,
            updated_at = datetime('now');
        `;
        await env.DB.prepare(sql).bind(experimentId, dateBucket).run();
      } catch (dbErr) {
        return new Response(JSON.stringify({ error: "Storage error recording telemetry" }), {
          status: 500,
          headers: { "Content-Type": "application/json", ...corsHeaders(request) },
        });
      }

      // Non-blocking 204 No Content for beacons
      return new Response(null, {
        status: 204,
        headers: corsHeaders(request),
      });
    }

    // ── 2. Telemetry Retrieval: /api/v1/telemetry/summary ───────────────────
    if (path === "/api/v1/telemetry/summary") {
      if (request.method !== "GET") {
        return new Response(JSON.stringify({ error: "Method not allowed" }), {
          status: 405,
          headers: { "Content-Type": "application/json", Allow: "GET" },
        });
      }

      // Strict Bearer token authentication
      const authHeader = request.headers.get("Authorization") || "";
      const expectedKey = env.TELEMETRY_RETRIEVAL_API_KEY;

      if (!expectedKey) {
        return new Response(JSON.stringify({ error: "Retrieval secret unconfigured" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }

      const match = authHeader.match(/^Bearer\s+(.+)$/i);
      const providedKey = match ? match[1].trim() : null;

      if (!providedKey || providedKey !== expectedKey) {
        return new Response(JSON.stringify({ error: "Unauthorized" }), {
          status: 401,
          headers: { "Content-Type": "application/json" },
        });
      }

      // Query parameters validation
      const experimentId = (url.searchParams.get("experiment_id") || "").trim();
      const dateStart = (url.searchParams.get("date_start") || "").trim();
      const dateStop = (url.searchParams.get("date_stop") || "").trim();

      if (!UUID_REGEX.test(experimentId)) {
        return new Response(JSON.stringify({ error: "Missing or invalid experiment_id parameter" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (!DATE_REGEX.test(dateStart) || !DATE_REGEX.test(dateStop)) {
        return new Response(JSON.stringify({ error: "date_start and date_stop must be YYYY-MM-DD" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }

      if (dateStop < dateStart) {
        return new Response(JSON.stringify({ error: "date_stop cannot be earlier than date_start" }), {
          status: 400,
          headers: { "Content-Type": "application/json" },
        });
      }

      try {
        const sql = `
          SELECT COALESCE(SUM(access_count), 0) AS total_count, MAX(updated_at) AS last_updated
          FROM guide_access_daily
          WHERE experiment_id = ?1 AND event_type = 'guide_access' AND date_bucket BETWEEN ?2 AND ?3;
        `;
        const result = await env.DB.prepare(sql).bind(experimentId, dateStart, dateStop).first();

        const count = result ? Number(result.total_count || 0) : 0;
        let updatedAt = null;
        if (result && result.last_updated) {
          // Normalize SQLite datetime string into ISO 8601 UTC
          const rawUpdated = String(result.last_updated);
          updatedAt = rawUpdated.includes("T") ? rawUpdated : rawUpdated.replace(" ", "T") + "Z";
        }

        const sourceReference = `edge:telemetry:guide_access:${experimentId}:${dateStart}:${dateStop}`;

        const payload = {
          experiment_id: experimentId,
          event_type: "guide_access",
          count: count,
          date_start: dateStart,
          date_stop: dateStop,
          source_reference: sourceReference,
          evidence_type: "fact",
          updated_at: updatedAt,
        };

        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
          },
        });
      } catch (dbErr) {
        return new Response(JSON.stringify({ error: "Storage query error" }), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        });
      }
    }

    // ── 3. Unmatched Routes ─────────────────────────────────────────────────
    return new Response(JSON.stringify({ error: "Not found" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  },
};
