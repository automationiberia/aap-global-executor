#!/usr/bin/env python3
"""Look up a user in Active Directory/LDAP and evaluate executor eligibility.

Requires: python-ldap (python3-ldap on RHEL). Compatible with Python 3.9+.

Usage:
  eval_ad_account.py --server URI --bind-dn DN --bind-password PW \\
      --search-base BASE --username SAM [--validate-certs true|false]

Always exits 0 and prints JSON: {"ok": bool, "reason": str, ...}
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict, Optional


def emit(payload: Dict[str, Any]) -> int:
    json.dump(payload, sys.stdout)
    return 0


def first_attr(entry_attrs: Dict[str, Any], name: str, default: str = "0") -> str:
    raw = entry_attrs.get(name)
    if raw is None:
        return default
    if isinstance(raw, (list, tuple)):
        if not raw:
            return default
        val = raw[0]
    else:
        val = raw
    if isinstance(val, bytes):
        val = val.decode("utf-8", errors="replace")
    return str(val)


def evaluate_entry(attrs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not attrs:
        return {"ok": False, "reason": "not_found"}

    uac = int(first_attr(attrs, "userAccountControl", "0"))
    pwd_last_set = first_attr(attrs, "pwdLastSet", "0")
    account_expires = int(first_attr(attrs, "accountExpires", "0"))
    lockout_time = int(first_attr(attrs, "lockoutTime", "0") or "0")

    # ADS_UF_ACCOUNTDISABLE = 0x0002
    if uac & 0x2:
        return {"ok": False, "reason": "disabled"}
    # ADS_UF_PASSWORD_EXPIRED = 0x800000
    if uac & 0x800000:
        return {"ok": False, "reason": "password_expired"}
    if pwd_last_set == "0":
        return {"ok": False, "reason": "password_expired"}

    never = {0, 9223372036854775807}
    if account_expires not in never:
        unix_exp = (account_expires / 10_000_000) - 11644473600
        if unix_exp < time.time():
            return {"ok": False, "reason": "account_expired"}

    if lockout_time != 0:
        return {"ok": False, "reason": "locked_out"}

    return {"ok": True, "reason": "ok"}


def normalize_server(server: str) -> str:
    server = (server or "").strip()
    if not server:
        return server
    if "://" not in server:
        return "ldap://%s" % server
    return server


def ldap_lookup(
    server: str,
    bind_dn: str,
    bind_password: str,
    search_base: str,
    username: str,
    validate_certs: bool,
) -> Dict[str, Any]:
    try:
        import ldap  # type: ignore
        import ldap.filter  # type: ignore
    except ImportError:
        return {
            "ok": False,
            "reason": "ldap_error",
            "detail": "python-ldap is not installed in the execution environment",
        }

    server = normalize_server(server)
    conn = ldap.initialize(server)
    conn.set_option(ldap.OPT_REFERRALS, 0)
    conn.set_option(ldap.OPT_NETWORK_TIMEOUT, 15)
    if server.lower().startswith("ldaps://"):
        if validate_certs:
            conn.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
        else:
            conn.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)
        conn.set_option(ldap.OPT_X_TLS_NEWCTX, 0)

    result = []
    try:
        conn.simple_bind_s(bind_dn, bind_password)
        filt = "(&(objectClass=user)(sAMAccountName=%s))" % ldap.filter.escape_filter_chars(
            username
        )
        attrs = [
            "sAMAccountName",
            "userAccountControl",
            "pwdLastSet",
            "accountExpires",
            "lockoutTime",
        ]
        result = conn.search_s(search_base, ldap.SCOPE_SUBTREE, filt, attrs)
    except ldap.LDAPError as exc:  # type: ignore[name-defined]
        return {"ok": False, "reason": "ldap_error", "detail": str(exc)}
    finally:
        try:
            conn.unbind_s()
        except Exception:
            pass

    for dn, entry_attrs in result or []:
        if dn is None:
            continue
        return evaluate_entry(entry_attrs)
    return evaluate_entry(None)


def main() -> int:
    try:
        import os

        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--server", required=True)
        parser.add_argument("--bind-dn", required=True)
        parser.add_argument("--bind-password", default="")
        parser.add_argument(
            "--bind-password-env",
            default="",
            help="Read bind password from this environment variable (preferred over --bind-password)",
        )
        parser.add_argument("--search-base", required=True)
        parser.add_argument("--username", required=True)
        parser.add_argument(
            "--validate-certs",
            default="false",
            choices=["true", "false", "True", "False", "yes", "no"],
        )
        parser.add_argument(
            "--outfile",
            default="",
            help="Optional path to write the JSON result (always also printed to stdout)",
        )
        args = parser.parse_args()
        validate = str(args.validate_certs).lower() in ("true", "yes", "1")

        bind_password = args.bind_password
        if args.bind_password_env:
            bind_password = os.environ.get(args.bind_password_env, "")
        if not bind_password:
            payload = {
                "ok": False,
                "reason": "ldap_error",
                "detail": "bind password missing (pass --bind-password or --bind-password-env)",
            }
        else:
            payload = ldap_lookup(
                server=args.server,
                bind_dn=args.bind_dn,
                bind_password=bind_password,
                search_base=args.search_base,
                username=args.username,
                validate_certs=validate,
            )

        if args.outfile:
            with open(args.outfile, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        return emit(payload)
    except Exception as exc:  # noqa: BLE001 - always return JSON to Ansible
        payload = {"ok": False, "reason": "ldap_error", "detail": str(exc)}
        try:
            # best-effort outfile even on unexpected errors
            outfile = ""
            for i, a in enumerate(sys.argv):
                if a == "--outfile" and i + 1 < len(sys.argv):
                    outfile = sys.argv[i + 1]
                    break
            if outfile:
                with open(outfile, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
        except Exception:
            pass
        return emit(payload)


if __name__ == "__main__":
    raise SystemExit(main())
