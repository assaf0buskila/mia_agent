"""Discovery shapes from the real OAuth response builder, without database or network."""

import json

from app.api.assistant.oauth import _metadata
from app.core.config import MiaEnv, Settings


def document(protected):
    settings = Settings(_env_file=None, env=MiaEnv.TEST, kill_switch=False,
                        assaf_os_assistant_enabled=True, assaf_os_backend="postgres",
                        public_base_url="https://mia.assafweb.com")
    return json.loads(_metadata(settings, protected=protected).body)


SHAPES = {"protected_resource": lambda: document(True),
          "authorization_server": lambda: document(False)}
