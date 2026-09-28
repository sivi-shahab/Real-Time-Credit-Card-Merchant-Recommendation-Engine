"""Superset for rec-engine: SSO through the Keycloak realm, one role per identity, read-only
access to the `analytics` views only (the database connection is `rec_analytics`)."""
import logging
import os

from flask_appbuilder.security.manager import AUTH_OAUTH
from superset.security import SupersetSecurityManager

log = logging.getLogger("superset.rec")

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"

AUTH_TYPE = AUTH_OAUTH
AUTH_USER_REGISTRATION = True
# FAB adds this to the mapped roles; Gamma is the base every mapped role already has
AUTH_USER_REGISTRATION_ROLE = "Gamma"
AUTH_ROLES_SYNC_AT_LOGIN = True
OAUTH_PROVIDERS = [{
    "name": "keycloak",
    "icon": "fa-key",
    "token_key": "access_token",
    "remote_app": {
        "client_id": os.environ["SUPERSET_OIDC_CLIENT_ID"],
        "client_secret": os.environ["SUPERSET_OIDC_CLIENT_SECRET"],
        # back channel; the browser is sent to the realm's public hostname
        "server_metadata_url": os.environ["SUPERSET_OIDC_DISCOVERY_URL"],
        "client_kwargs": {"scope": "openid profile", "code_challenge_method": "S256"},
    },
}]

# Application role (Keycloak realm role) -> Superset roles. Analysts and ML engineers
# explore and build charts; everyone else reads dashboards.
READERS = ["Gamma", "Analytics Reader"]
AUTH_ROLES_MAPPING = {
    "Platform Operator": ["Admin"],
    "Analyst": ["Alpha", "sql_lab"],
    "ML Engineer": ["Alpha", "sql_lab"],
    "Marketing Operator": READERS,
    "Approver": READERS,
    "Auditor": READERS,
    "Viewer": READERS,
}
FAB_ROLES = {"Analytics Reader": [["all_datasource_access", "all_datasource_access"],
                                  ["all_database_access", "all_database_access"]]}


class KeycloakSecurityManager(SupersetSecurityManager):
    def oauth_user_info(self, provider, response=None):
        me = self.oauth_remotes[provider].userinfo()
        roles = [r for r in me.get("roles", []) if r in AUTH_ROLES_MAPPING]
        if len(roles) != 1:  # SEC-001: one identity, one role, as in the dashboard
            log.warning("refused %s: application roles %s", me.get("preferred_username"),
                        roles)
            return {}
        username = me["preferred_username"]
        return {"username": username, "first_name": me.get("given_name") or username,
                "last_name": me.get("family_name") or "",
                "email": me.get("email") or f"{username}@rec.local", "role_keys": roles}


CUSTOM_SECURITY_MANAGER = KeycloakSecurityManager

FEATURE_FLAGS = {"DASHBOARD_NATIVE_FILTERS": True}
ROW_LIMIT = 10000
