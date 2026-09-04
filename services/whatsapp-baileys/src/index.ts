/**
 * WhatsApp Web sidecar for Mia.
 *
 * Baileys speaks the reverse-engineered WhatsApp Web protocol and is Node-only, so the
 * socket lives here rather than in Mia's Python process. Two directions:
 *
 *   inbound   WhatsApp -> this process -> POST {MIA_URL}/v1/whatsapp/baileys/webhook
 *   outbound  Mia -> POST /send on this process -> WhatsApp
 *
 * Both directions carry the same shared token in an Authorization header. There is no
 * other authentication, so this port must never be exposed publicly.
 *
 * This is not the official WhatsApp Business API. Using it puts the linked number at
 * risk of being banned. Pair a number you can afford to lose before pointing it at the
 * number the business runs on.
 */
import { createServer } from "node:http";
import { Boom } from "@hapi/boom";
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  type WASocket,
} from "@whiskeysockets/baileys";
import pino from "pino";

const log = pino({ level: process.env.LOG_LEVEL ?? "info" });

const PORT = Number(process.env.PORT ?? 8088);
const MIA_URL = (process.env.MIA_URL ?? "").replace(/\/$/, "");
const TOKEN = process.env.MIA_WHATSAPP_BAILEYS_TOKEN ?? "";
// Session state. Losing this directory means pairing the phone again by QR.
const AUTH_DIR = process.env.BAILEYS_AUTH_DIR ?? "./auth";

if (!TOKEN) {
  log.error("MIA_WHATSAPP_BAILEYS_TOKEN is required; refusing to start");
  process.exit(1);
}
if (!MIA_URL) {
  log.error("MIA_URL is required; refusing to start");
  process.exit(1);
}

let sock: WASocket | undefined;
let connected = false;

/** Digits only, as WhatsApp JIDs carry no punctuation. */
function toJid(raw: string): string {
  if (raw.includes("@")) return raw;
  return `${raw.replace(/\D/g, "")}@s.whatsapp.net`;
}

/** JID back to the bare number Mia keys its leads on. */
function fromJid(jid: string): string {
  return jid.split("@")[0] ?? jid;
}

async function forwardInbound(messages: Array<Record<string, unknown>>): Promise<void> {
  if (messages.length === 0) return;
  try {
    const response = await fetch(`${MIA_URL}/v1/whatsapp/baileys/webhook`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ messages }),
    });
    if (!response.ok) {
      // Do not retry. Mia dedupes on message id, and a retry loop against a failing
      // Mia would replay the same customer messages repeatedly.
      log.warn({ status: response.status }, "mia rejected inbound batch");
    }
  } catch (error) {
    log.warn({ err: error }, "could not reach mia");
  }
}

async function connect(): Promise<void> {
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  sock = makeWASocket({ auth: state, printQRInTerminal: true });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) log.info("scan the QR above with WhatsApp > Linked devices");
    if (connection === "open") {
      connected = true;
      log.info("whatsapp connected");
    }
    if (connection === "close") {
      connected = false;
      const status = (lastDisconnect?.error as Boom | undefined)?.output?.statusCode;
      const loggedOut = status === DisconnectReason.loggedOut;
      log.warn({ status, loggedOut }, "whatsapp disconnected");
      if (loggedOut) {
        // The phone unlinked this device. Reconnecting cannot fix it; the auth
        // directory has to be cleared and the QR scanned again.
        log.error("logged out: clear the auth directory and pair again");
        return;
      }
      setTimeout(() => void connect(), 3000);
    }
  });

  sock.ev.on("messages.upsert", ({ messages, type }) => {
    if (type !== "notify") return;
    const batch: Array<Record<string, unknown>> = [];
    for (const message of messages) {
      // Skip our own sends and anything without plain text.
      if (message.key.fromMe) continue;
      const jid = message.key.remoteJid ?? "";
      // Groups and broadcasts are not customer conversations.
      if (!jid.endsWith("@s.whatsapp.net")) continue;
      const text =
        message.message?.conversation ??
        message.message?.extendedTextMessage?.text ??
        "";
      if (!text.trim()) continue;
      batch.push({ id: message.key.id ?? "", from: fromJid(jid), text });
    }
    void forwardInbound(batch);
  });
}

function unauthorized(authorization: string | undefined): boolean {
  const presented = (authorization ?? "").replace(/^Bearer /, "").trim();
  return presented !== TOKEN;
}

createServer((req, res) => {
  const reply = (code: number, body: unknown): void => {
    res.writeHead(code, { "Content-Type": "application/json" });
    res.end(JSON.stringify(body));
  };

  if (req.method === "GET" && req.url === "/health") {
    return reply(200, { status: "ok", connected });
  }

  if (req.method === "POST" && req.url === "/send") {
    if (unauthorized(req.headers.authorization)) return reply(401, { sent: false });
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      // Mia sends short text. Anything larger is not ours.
      if (raw.length > 100_000) req.destroy();
    });
    req.on("end", () => {
      void (async () => {
        try {
          const { to, text } = JSON.parse(raw || "{}") as {
            to?: string;
            text?: string;
          };
          if (!to || !text) return reply(400, { sent: false, error: "to and text required" });
          if (!sock || !connected) return reply(503, { sent: false, error: "not connected" });
          await sock.sendMessage(toJid(to), { text });
          reply(200, { sent: true });
        } catch (error) {
          log.warn({ err: error }, "send failed");
          reply(502, { sent: false });
        }
      })();
    });
    return;
  }

  reply(404, { error: "not found" });
}).listen(PORT, () => log.info({ port: PORT }, "baileys sidecar listening"));

void connect();
