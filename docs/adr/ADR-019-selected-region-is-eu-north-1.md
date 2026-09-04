# ADR-019 Selected Region is eu-north-1

- **Status:** accepted
- **Date:** 2026-08-22
- **Assaf:** ADOPT (chat: Bible `il-central-1` is old; live project Region is `eu-north-1`)

**Context**
The new AWS experience pins one selected Region from the contact address. This project can create Regional resources only in `eu-north-1`. The Bible still said `il-central-1`. Live Fargate, RDS, ALB, ACM, and Secrets Manager already run in `eu-north-1`.

**Decision**
Selected Region for Mia is **`eu-north-1`**. Do not create Lambda, RDS, ECS, or other Regional resources elsewhere. CloudFront remains global. A later Bible-file cleanup pass is a separate Assaf request — this ADR does not delete historical reports.

**Consequences**
Operator scripts and `docs/PRODUCTION_BUILD.md` pin `eu-north-1`. `CapabilityId.AWS_RUNTIME` stays specified until `app.infra` exists. Rekognition/Textract/Personalize/App Runner reduced availability in this Region does not affect current Mia ports.

**Alternatives considered**
Move the project to an account that can use `il-central-1` — rejected; live host already works in `eu-north-1`. Multi-Region — excluded by the new AWS experience.
