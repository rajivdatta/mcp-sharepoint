"""One-time interactive sign-in.

Run this from a real terminal ONCE. It pops the Windows account broker, writes
`.auth_record.json`, and seeds the DPAPI-encrypted MSAL cache. After that the
MCP server - which starts headless, with no window to parent a dialog to - gets
tokens silently.

    .venv\\Scripts\\python.exe setup_auth.py
"""
import json
import sys

import graph

print(f"tenant : {graph.config().get('tenant_id')}")
print(f"account: {graph.config().get('login_hint')}")
print(f"scope  : {graph.SCOPE}\nsigning in ...\n")

try:
    who = graph.whoami()
    print(json.dumps(who, indent=2))
    site = graph.resolve_site()
    print(f"\ndefault site resolved: {site['displayName']}  {site['webUrl']}")
    print("\nlibraries on that site:")
    for d in graph.list_drives():
        print(f"  - {d['name']:<35} {d['quotaUsedMB']:>10,.0f} MB  {d['driveType']}")
    print("\nSign-in cached. Start the MCP server now.")
except Exception as exc:
    print(f"FAILED: {type(exc).__name__}: {exc}")
    sys.exit(1)
