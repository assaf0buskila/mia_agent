"""Reviewed owner auth/read surface plus the explicit Business TEST router."""

from app.api.business_ledger import router as business_ledger_router
from staging_runtime.base_api import create_app as create_base_app
from staging_runtime.settings import load_stage_config

stage_config = load_stage_config()
app = create_base_app(stage_config)
app.include_router(business_ledger_router)
