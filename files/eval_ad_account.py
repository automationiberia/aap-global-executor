#!/usr/bin/env python3
"""Look up a user in Active Directory/LDAP and evaluate executor eligibility.

Requires: python-ldap (python3-ldap on RHEL).

Usage:
  eval_ad_account.py --server URI --bind-dn DN --bind-password PW \\
      --search-base BASE --username SAM [--validate-certs true|false]

Prints JSON: {"ok": bool, "reason": str}
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def first_attr(entry_attrs: dict, name: str, default: str = "0") -> str:
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


def evaluate_entry(attrs: dict | None) -> dict:
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


def ldap_lookup(
    server: str,
    bind_dn: str,
    bind_password: str,
    search_base: str,
    username: str,
    validate_certs: bool,
) -> dict:
    try:
        import ldap  # type: ignore
        import ldap.filter  # type: ignore
    except ImportError:
        return {
            "ok": False,
            "reason": "ldap_error",
            "detail": "python-ldap is not installed in the execution environment",
        }

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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--bind-dn", required=True)
    parser.add_argument("--bind-password", required=True)
    parser.add_argument("--search-base", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--validate-certs",
        default="false",
        choices=["true", "false", "True", "False", "yes", "no"],
    )
    args = parser.parse_args()
    validate = str(args.validate_certs).lower() in ("true", "yes", "1")

    result = ldap_lookup(
        server=args.server,
        bind_dn=args.bind_dn,
        bind_password=args.bind_password,
        search_base=args.search_base,
        username=args.username,
        validate_certs=validate,
    )
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
