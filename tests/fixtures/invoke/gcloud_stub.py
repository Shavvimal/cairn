# Stub `gcloud` for tests/test_invoke.py: only `auth application-default
# print-access-token` is modelled. STUB_GCLOUD=ok (default) prints a token and exits
# 0; rapt fails the way a Workspace session policy expiry does with no terminal (the
# probe's case: gcloud cannot start its reauth prompt); invalid-grant is the same
# rejection as gcloud reports it with a terminal (seen live on 2026-10-27); fail is a
# transport failure, which the probe must not treat as a rejection.
import os
import sys

mode = os.environ.get("STUB_GCLOUD", "ok")
if sys.argv[1:4] != ["auth", "application-default", "print-access-token"]:
    print(f"stub gcloud: unexpected argv {sys.argv[1:]}", file=sys.stderr)
    sys.exit(2)
if mode == "rapt":
    print(
        "ERROR: (gcloud.auth.application-default.print-access-token) Reauthentication "
        "failed. cannot prompt during non-interactive execution.",
        file=sys.stderr,
    )
    sys.exit(1)
if mode == "invalid-grant":
    print(
        "ERROR: (gcloud.auth.application-default.print-access-token) There was a problem "
        "refreshing your current auth tokens: ('invalid_grant: reauth related error "
        "(invalid_rapt)', {'error': 'invalid_grant', 'error_description': 'reauth related "
        "error (invalid_rapt)', 'error_subtype': 'invalid_rapt'})",
        file=sys.stderr,
    )
    sys.exit(1)
if mode == "fail":
    print("ERROR: could not resolve host oauth2.googleapis.com", file=sys.stderr)
    sys.exit(1)
print("ya29.stub-token")
