"""Run with an immutable installation's Python -I, as the dedicated custody UID."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "server"))
if len(sys.argv) < 2 or sys.argv[1] not in {"daemon", "admin"}:
    raise SystemExit("Use: python -I custody-service.py {daemon|admin} --config /etc/mangrove-custody/policy.json ...")
mode = sys.argv.pop(1)
if mode == "daemon":
    from src.custody.daemon import main
else:
    from src.custody.admin import main
try:
    main()
except Exception:  # noqa: BLE001 - secret-bearing exception text must not escape administration
    raise SystemExit("Custody refused or failed. Check protected installation, policy and audit state in the administrator terminal; secret details are not included in this error.") from None
