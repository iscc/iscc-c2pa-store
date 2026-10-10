"""Create an operator token for deletes: print the token (keep it) and its SHA-256 digest (configure it).

Usage:

    uv run python scripts/operator_token.py
"""

import hashlib
import secrets

if __name__ == "__main__":
    token = secrets.token_urlsafe(32)
    print(f"Operator token (keep it secret):           {token}")
    print(f"ISCC_C2PA_STORE_OPERATOR_TOKEN_SHA256 = {hashlib.sha256(token.encode()).hexdigest()}")
