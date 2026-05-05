#!/usr/bin/env python3
"""
Salesforce to Veza OAA Integration Script

Collects identity and permission data from Salesforce via the REST API
(OAuth 2.0 Client Credentials) and pushes it to Veza as a CustomApplication.

Entities modelled:
  Local User       — Salesforce User records
  Local Role       — Salesforce Profiles and standalone PermissionSets
  Application Resource — Salesforce SObject types (e.g. Account, Contact)
  Custom Permission    — read / create / edit / delete / view_all / modify_all
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from oaaclient.client import OAAClient, OAAClientError
from oaaclient.templates import CustomApplication, OAAPermission

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(log_level: str = "INFO") -> None:
    """Configure file-only logging with hourly rotation to the logs/ folder."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.join(script_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%d%m%Y-%H%M")
    script_name = os.path.splitext(os.path.basename(__file__))[0]
    log_file = os.path.join(log_dir, f"{script_name}_{timestamp}.log")

    handler = TimedRotatingFileHandler(
        log_file,
        when="h",
        interval=1,
        backupCount=24,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.addHandler(handler)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Salesforce to Veza OAA Integration",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # Veza connection
    parser.add_argument("--veza-url", default=None,
                        help="Veza tenant URL (or VEZA_URL env var)")
    parser.add_argument("--veza-api-key", default=None,
                        help="Veza API key (or VEZA_API_KEY env var)")

    # Salesforce connection
    parser.add_argument("--sf-instance-url", default=None,
                        help="Salesforce instance URL, e.g. https://myorg.my.salesforce.com "
                             "(or SF_INSTANCE_URL env var)")
    parser.add_argument("--sf-token-url", default=None,
                        help="Salesforce OAuth2 token endpoint URL (or SF_TOKEN_URL env var)")
    parser.add_argument("--sf-client-id", default=None,
                        help="Salesforce connected app client ID (or SF_CLIENT_ID env var)")
    parser.add_argument("--sf-client-secret", default=None,
                        help="Salesforce connected app client secret (or SF_CLIENT_SECRET env var)")
    parser.add_argument("--sf-api-version", default=None,
                        help="Salesforce REST API version (or SF_API_VERSION env var, default: 60.0)")

    # OAA provider settings
    parser.add_argument("--provider-name", default="SalesforceUAT",
                        help="Provider name as shown in Veza")
    parser.add_argument("--datasource-name", default=None,
                        help="Datasource name in Veza (defaults to SF instance hostname)")

    # Run options
    parser.add_argument("--data-dir", default=None,
                        help="(Unused for API connector — data is fetched live from Salesforce)")
    parser.add_argument("--env-file", default=".env",
                        help="Path to .env file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build OAA payload without pushing to Veza")
    parser.add_argument("--save-json", action="store_true",
                        help="Save OAA payload as JSON file for inspection")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging verbosity")

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(args: argparse.Namespace) -> dict:
    """Load configuration: CLI arg > env var > .env file."""
    if args.env_file and os.path.exists(args.env_file):
        load_dotenv(args.env_file)

    sf_instance_url = (args.sf_instance_url or os.getenv("SF_INSTANCE_URL", "")).rstrip("/")
    sf_api_version = args.sf_api_version or os.getenv("SF_API_VERSION", "60.0")

    config = {
        "veza_url":        args.veza_url or os.getenv("VEZA_URL"),
        "veza_api_key":    args.veza_api_key or os.getenv("VEZA_API_KEY"),
        "sf_instance_url": sf_instance_url,
        "sf_token_url":    args.sf_token_url or os.getenv("SF_TOKEN_URL"),
        "sf_client_id":    args.sf_client_id or os.getenv("SF_CLIENT_ID"),
        "sf_client_secret":args.sf_client_secret or os.getenv("SF_CLIENT_SECRET"),
        "sf_api_version":  sf_api_version,
    }

    missing = []
    for key in ("sf_instance_url", "sf_token_url", "sf_client_id", "sf_client_secret"):
        if not config[key]:
            missing.append(key.upper())

    if not args.dry_run:
        for key in ("veza_url", "veza_api_key"):
            if not config[key]:
                missing.append(key.upper())

    if missing:
        for key in missing:
            log.error("Missing required configuration: %s", key)
        sys.exit(1)

    return config


# ---------------------------------------------------------------------------
# Salesforce REST API client
# ---------------------------------------------------------------------------

class SalesforceClient:
    """Thin HTTP client for the Salesforce REST API (OAuth2 Client Credentials)."""

    def __init__(self, instance_url: str, token_url: str, client_id: str,
                 client_secret: str, api_version: str = "60.0") -> None:
        self.instance_url = instance_url
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_version = api_version
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def authenticate(self) -> None:
        """Obtain an access token using the OAuth2 client_credentials grant."""
        log.info("Authenticating to Salesforce via OAuth2 client_credentials")
        response = self._session.post(
            self.token_url,
            data={
                "grant_type":    "client_credentials",
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        token_data = response.json()
        access_token = token_data.get("access_token")
        if not access_token:
            log.error("No access_token in Salesforce OAuth2 response: %s",
                      list(token_data.keys()))
            sys.exit(1)
        self._session.headers.update({"Authorization": f"Bearer {access_token}"})
        log.info("Salesforce authentication successful")

    def _query(self, soql: str) -> list:
        """Execute a SOQL query and return all records (handles server-side pagination)."""
        url = f"{self.instance_url}/services/data/v{self.api_version}/query"
        params: dict = {"q": soql}
        records: list = []
        while True:
            response = self._session.get(url, params=params, timeout=60)
            response.raise_for_status()
            data = response.json()
            records.extend(data.get("records", []))
            if data.get("done", True):
                break
            next_url = data.get("nextRecordsUrl", "")
            url = f"{self.instance_url}{next_url}"
            params = {}
        return records

    def get_users(self) -> list:
        """Fetch standard-type Salesforce users."""
        log.info("Fetching Salesforce users")
        soql = (
            "SELECT Id, Username, Email, FirstName, LastName, IsActive, ProfileId, UserType "
            "FROM User "
            "WHERE UserType IN ('Standard', 'PowerCustomerSuccess', 'PowerPartner', "
            "'CustomerSuccess', 'CsnOnly') "
            "ORDER BY Username"
        )
        users = self._query(soql)
        log.info("Fetched %d users", len(users))
        return users

    def get_profiles(self) -> list:
        """Fetch all Salesforce Profiles."""
        log.info("Fetching Salesforce profiles")
        profiles = self._query(
            "SELECT Id, Name, UserType, Description FROM Profile ORDER BY Name"
        )
        log.info("Fetched %d profiles", len(profiles))
        return profiles

    def get_all_permission_sets(self) -> list:
        """Fetch all PermissionSets (including profile-owned) for ID-to-name mapping."""
        log.info("Fetching all Salesforce permission sets")
        soql = (
            "SELECT Id, Name, Label, IsOwnedByProfile, ProfileId, Description "
            "FROM PermissionSet ORDER BY Name"
        )
        perm_sets = self._query(soql)
        log.info("Fetched %d permission sets", len(perm_sets))
        return perm_sets

    def get_permission_set_assignments(self) -> list:
        """Fetch PermissionSet assignments for non-profile permission sets only."""
        log.info("Fetching permission set assignments")
        soql = (
            "SELECT Id, AssigneeId, PermissionSetId "
            "FROM PermissionSetAssignment "
            "WHERE PermissionSet.IsOwnedByProfile = false"
        )
        assignments = self._query(soql)
        log.info("Fetched %d permission set assignments", len(assignments))
        return assignments

    def get_object_permissions(self) -> list:
        """Fetch object-level CRUD permissions across all permission sets."""
        log.info("Fetching object permissions")
        soql = (
            "SELECT Id, ParentId, SObjectType, "
            "PermissionsRead, PermissionsCreate, PermissionsEdit, PermissionsDelete, "
            "PermissionsViewAllRecords, PermissionsModifyAllRecords "
            "FROM ObjectPermissions"
        )
        perms = self._query(soql)
        log.info("Fetched %d object permission records", len(perms))
        return perms


# ---------------------------------------------------------------------------
# OAA payload builder
# ---------------------------------------------------------------------------

def build_oaa_payload(
    users: list,
    profiles: list,
    all_permission_sets: list,
    perm_set_assignments: list,
    object_permissions: list,
    provider_name: str,
    datasource_name: str,
) -> CustomApplication:
    """Assemble the Veza OAA CustomApplication from Salesforce data."""

    app = CustomApplication(name=datasource_name, application_type=provider_name)

    # Map Salesforce CRUD flags to OAA permission types
    app.add_custom_permission("read",       [OAAPermission.DataRead])
    app.add_custom_permission("create",     [OAAPermission.DataRead, OAAPermission.DataWrite])
    app.add_custom_permission("edit",       [OAAPermission.DataRead, OAAPermission.DataWrite])
    app.add_custom_permission("delete",     [OAAPermission.DataRead, OAAPermission.DataWrite])
    app.add_custom_permission("view_all",   [OAAPermission.DataRead, OAAPermission.MetadataRead])
    app.add_custom_permission("modify_all", [
        OAAPermission.DataRead, OAAPermission.DataWrite,
        OAAPermission.MetadataRead, OAAPermission.MetadataWrite,
    ])

    # Build profile ID → name index
    profile_id_to_name: dict = {p["Id"]: p["Name"] for p in profiles}

    # Build PermissionSet ID → OAA role name map.
    # Profile-owned PermissionSets resolve back to their parent Profile's name so that
    # ObjectPermissions assigned to a profile are attributed to the Profile role.
    ps_id_to_role_name: dict = {}
    standalone_ps: list = []
    for ps in all_permission_sets:
        if ps.get("IsOwnedByProfile") and ps.get("ProfileId"):
            profile_name = profile_id_to_name.get(ps["ProfileId"])
            if profile_name:
                ps_id_to_role_name[ps["Id"]] = profile_name
        else:
            role_name = ps.get("Label") or ps["Name"]
            ps_id_to_role_name[ps["Id"]] = role_name
            standalone_ps.append(ps)

    # ---- OAA Local Roles: Profiles ----
    log.info("Adding %d profiles as local roles", len(profiles))
    added_role_names: set = set()
    for profile in profiles:
        app.add_local_role(profile["Name"])
        added_role_names.add(profile["Name"])

    # ---- OAA Local Roles: Standalone Permission Sets ----
    log.info("Adding %d standalone permission sets as local roles", len(standalone_ps))
    for ps in standalone_ps:
        role_name = ps.get("Label") or ps["Name"]
        if role_name in added_role_names:
            log.warning("Skipping duplicate local role %r (PermissionSet Id: %s)", role_name, ps.get("Id", "unknown"))
            continue
        added_role_names.add(role_name)
        app.add_local_role(role_name)

    # ---- OAA Local Users ----
    log.info("Adding %d users as local users", len(users))
    user_id_to_username: dict = {}
    for user in users:
        username = user["Username"]
        local_user = app.add_local_user(
            name=username,
            identities=[user["Email"]] if user.get("Email") else [],
        )
        local_user.is_active = bool(user.get("IsActive", True))

        parts = [user.get("FirstName"), user.get("LastName")]
        full_name = " ".join(p for p in parts if p).strip()
        if full_name:
            local_user.full_name = full_name

        # Assign the user's Profile as a role
        profile_name = profile_id_to_name.get(user.get("ProfileId", ""))
        if profile_name:
            local_user.add_role(profile_name)

        user_id_to_username[user["Id"]] = username

    # ---- Permission Set Assignments → additional role memberships ----
    log.info("Processing %d permission set assignments", len(perm_set_assignments))
    for assignment in perm_set_assignments:
        username = user_id_to_username.get(assignment["AssigneeId"])
        role_name = ps_id_to_role_name.get(assignment["PermissionSetId"])
        if username and role_name:
            local_user = app.local_users.get(username)
            if local_user:
                local_user.add_role(role_name)
            else:
                log.debug("User %s not found when assigning PS role %s", username, role_name)

    # ---- Application Resources: unique Salesforce SObject types ----
    sobject_types = sorted({op["SObjectType"] for op in object_permissions if op.get("SObjectType")})
    log.info("Adding %d Salesforce object types as application resources", len(sobject_types))
    resource_map: dict = {}
    for sobj_type in sobject_types:
        resource_map[sobj_type] = app.add_resource(sobj_type, resource_type="SObject")

    # ---- Role → Resource permissions from ObjectPermissions ----
    log.info("Mapping object permissions to roles and resources")
    mapped = 0
    skipped = 0
    for op in object_permissions:
        role_name = ps_id_to_role_name.get(op.get("ParentId", ""))
        sobj_type = op.get("SObjectType")
        if not sobj_type or not role_name:
            skipped += 1
            continue
        resource = resource_map.get(sobj_type)
        if resource is None:
            skipped += 1
            continue
        role = app.local_roles.get(role_name)
        if role is None:
            skipped += 1
            continue

        perm_flags = {
            "read":       op.get("PermissionsRead", False),
            "create":     op.get("PermissionsCreate", False),
            "edit":       op.get("PermissionsEdit", False),
            "delete":     op.get("PermissionsDelete", False),
            "view_all":   op.get("PermissionsViewAllRecords", False),
            "modify_all": op.get("PermissionsModifyAllRecords", False),
        }
        for perm_name, granted in perm_flags.items():
            if granted:
                role.add_permission(perm_name, resources=[resource])
                mapped += 1

    log.info("Mapped %d object permissions; skipped %d unmapped entries", mapped, skipped)
    return app


# ---------------------------------------------------------------------------
# Veza push
# ---------------------------------------------------------------------------

def push_to_veza(
    veza_url: str,
    veza_api_key: str,
    provider_name: str,
    datasource_name: str,
    app: CustomApplication,
    dry_run: bool = False,
) -> None:
    """Push the OAA payload to Veza (or log-only if --dry-run)."""
    if dry_run:
        log.info("[DRY RUN] Payload built successfully — skipping Veza push")
        return

    veza_con = OAAClient(url=veza_url, token=veza_api_key)
    try:
        response = veza_con.push_application(
            provider_name=provider_name,
            data_source_name=datasource_name,
            application_object=app,
            create_provider=True,
        )
        if response and response.get("warnings"):
            for w in response["warnings"]:
                log.warning("Veza warning: %s", w)
        log.info("Successfully pushed to Veza: provider=%s datasource=%s",
                 provider_name, datasource_name)
    except OAAClientError as exc:
        log.error("Veza push failed: %s — %s (HTTP %s)",
                  exc.error, exc.message, exc.status_code)
        if hasattr(exc, "details"):
            for detail in exc.details:
                log.error("  Detail: %s", detail)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()
    _setup_logging(args.log_level)

    print("=" * 60)
    print("  Salesforce → Veza OAA Integration")
    print(f"  Log level : {args.log_level}")
    print(f"  Dry run   : {args.dry_run}")
    print("=" * 60)

    config = load_config(args)

    # Derive datasource name from the instance hostname if not provided
    datasource_name = args.datasource_name
    if not datasource_name:
        hostname = urlparse(config["sf_instance_url"]).hostname or config["sf_instance_url"]
        datasource_name = hostname

    log.info("Starting Salesforce OAA integration: provider=%s datasource=%s",
             args.provider_name, datasource_name)

    # Authenticate and collect data from Salesforce
    sf = SalesforceClient(
        instance_url=config["sf_instance_url"],
        token_url=config["sf_token_url"],
        client_id=config["sf_client_id"],
        client_secret=config["sf_client_secret"],
        api_version=config["sf_api_version"],
    )
    sf.authenticate()

    users               = sf.get_users()
    profiles            = sf.get_profiles()
    all_permission_sets = sf.get_all_permission_sets()
    perm_set_assignments = sf.get_permission_set_assignments()
    object_permissions  = sf.get_object_permissions()

    log.info(
        "Data collection complete: %d users, %d profiles, %d permission sets, "
        "%d assignments, %d object permissions",
        len(users), len(profiles), len(all_permission_sets),
        len(perm_set_assignments), len(object_permissions),
    )

    # Build OAA payload
    app = build_oaa_payload(
        users=users,
        profiles=profiles,
        all_permission_sets=all_permission_sets,
        perm_set_assignments=perm_set_assignments,
        object_permissions=object_permissions,
        provider_name=args.provider_name,
        datasource_name=datasource_name,
    )

    # Save JSON payload if requested
    if args.save_json or args.dry_run:
        payload = app.get_payload()
        out_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            f"salesforce_oaa_payload_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        log.info("OAA payload saved: %s", out_path)
        print(f"Payload saved: {out_path}")

    # Push to Veza
    push_to_veza(
        veza_url=config.get("veza_url", ""),
        veza_api_key=config.get("veza_api_key", ""),
        provider_name=args.provider_name,
        datasource_name=datasource_name,
        app=app,
        dry_run=args.dry_run,
    )

    log.info("Integration complete")


if __name__ == "__main__":
    main()
