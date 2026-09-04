# WhatsApp Baileys sidecar

Built, not deployed. Nothing runs this yet.

Baileys speaks the **reverse-engineered WhatsApp Web protocol** and is Node-only, so
the socket lives in this process rather than in Mia's Python app. Mia talks to it over
HTTP on a private port.

```
inbound    WhatsApp -> sidecar -> POST {MIA_URL}/v1/whatsapp/baileys/webhook
outbound   Mia -> POST /send on the sidecar -> WhatsApp
```

Both directions carry the same shared token. There is no other authentication, so
**this port must never be exposed publicly.**

## Read this before pointing it at a real number

This is not the official WhatsApp Business API. The Baileys documentation states it is
not affiliated with WhatsApp or Meta and that use "carries inherent risk at user
discretion" — that risk is the linked number being banned.

Pair a number you can afford to lose first. Move to the number the business runs on
only after it has survived several deploys.

## Run it locally

```
npm install
npm run build

export MIA_URL=http://localhost:8000
export MIA_WHATSAPP_BAILEYS_TOKEN=<same value Mia has>
npm start
```

A QR code prints on first start. Scan it from WhatsApp on the phone under
**Settings > Linked devices**. Credentials are then written to `./auth` and reused, so
the QR is a one-time step per number.

Then point Mia at it:

```
MIA_WHATSAPP_SENDER=baileys
MIA_WHATSAPP_BAILEYS_URL=http://localhost:8088
MIA_WHATSAPP_BAILEYS_TOKEN=<same value>
```

Without all three, `build_whatsapp_port` returns `DisabledMessagePort` and the inbound
webhook rejects every request. It fails closed.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8088` | Port the sidecar listens on |
| `MIA_URL` | — | Required. Where to post inbound messages |
| `MIA_WHATSAPP_BAILEYS_TOKEN` | — | Required. Shared token, both directions |
| `BAILEYS_AUTH_DIR` | `./auth` | Where session credentials live |
| `LOG_LEVEL` | `info` | pino level |

## Things that will bite you on the way to production

**The session must outlive a deploy.** `./auth` is a directory on local disk. On
Fargate that disappears when the task is replaced, which means a QR scan after every
deploy. Before deploying, that state needs somewhere durable — an EFS mount, or
serialising it into Secrets Manager on `creds.update`.

**One connection, one process.** WhatsApp Web allows a limited number of linked
devices and this design assumes a single sidecar. Do not scale it to two tasks.

**Logged out is terminal.** If the phone unlinks the device, reconnecting cannot fix
it: the auth directory has to be cleared and the QR scanned again. The sidecar detects
this and stops retrying rather than looping.

**Groups and broadcasts are ignored** — only direct chats reach Mia. That is
deliberate; a group message is not a customer conversation.

**Inbound is not retried.** Mia dedupes on message id, so a retry loop against a
failing Mia would replay the same customer messages. A failed forward is logged and
dropped.
