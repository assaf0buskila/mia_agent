"""Discovery shapes from the real OAuth response builder, without database or network."""

import json

from app.api.assistant.oauth import _metadata
from app.core.config import MiaEnv, Settings
from app.services import assistant_oauth as oauth
from tests.services.os_ledger.assistant.support import IP_A, AWorld


def document(protected):
    settings = Settings(_env_file=None, env=MiaEnv.TEST, kill_switch=False,
                        assaf_os_assistant_enabled=True, assaf_os_backend="postgres",
                        public_base_url="https://mia.assafweb.com")
    return json.loads(_metadata(settings, protected=protected).body)


def consent_pending():
    aw = AWorld("fake", public_base_url="https://mia.assafweb.com")
    service = oauth.AssistantOAuthService(aw.w.ledger, aw.config, revoke_watermark=lambda _: None)
    with service._tx(aw.w.owner) as tx:
        row = oauth.OAuthRequest(
            workspace_id=tx.ctx.workspace, oauth_request_id="OR_" + "a" * 32,
            owner_id=str(tx.ctx.numeric_owner), client_id=oauth.CLIENT_ID,
            redirect_uri=oauth.REDIRECT_URI, code_challenge="a" * 43, scope=oauth.SCOPE,
            resource=oauth.resource(aw.config), state="synthetic-state", epoch=1,
            status="pending_owner", created_at=tx.now, deadline=tx.now + oauth.REQUEST_TTL,
            client_ip_hmac=aw.ip_hmac(IP_A))
        oauth.OAuthTables(tx).insert(row)
    return service.status(aw.w.reader, row.oauth_request_id, chain_id="synthetic-chain")


SHAPES = {"protected_resource": lambda: document(True),
          "authorization_server": lambda: document(False), "consent_pending": consent_pending}
