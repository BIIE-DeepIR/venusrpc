"""Quick smoke test for the HTTP-based Connection against a real Venus runtime."""
import sys
sys.path.insert(0, "src")

from pathlib import Path

from venusrpc.connection import Connection, VenusError, VenusTransportError

LOGFILES_DIR = Path(r"C:\Program Files (x86)\HAMILTON\LogFiles")


def get_latest_trace() -> Path | None:
    """Return the most recently modified .trc file in the Hamilton LogFiles dir."""
    if not LOGFILES_DIR.exists():
        return None
    traces = list(LOGFILES_DIR.rglob("*.trc"))
    if not traces:
        return None
    return max(traces, key=lambda p: p.stat().st_mtime)

print("Starting Venus via HTTP connection...")
try:
    with Connection(start_minimized=False, accept_timeout=60.0) as con:
        print(f"  /ready received — Venus is up")

        # First: test direct assignment (bypass AddJsonVariable) with Trace
        result = con.execute(
            definitions="variable ans(0);",
            hsl_code='ans = 7 * 6; Trace("SCRATCH: ans=", ans, " json=", ___JSON___); ___JSON___ = ___JSON___ + "\\"value\\":" + IStr(ans) + ","; Trace("SCRATCH: after assign json=", ___JSON___);',
        )
        print(f"  direct assign raw result: {result!r}")

        # Second: test AddJsonVariable
        result = con.execute(
            definitions="variable ans(0);",
            hsl_code='ans = 7 * 6; AddJsonVariable(___JSON___, ans, "value");',
        )
        print(f"  AddJsonVariable raw result: {result!r}")
        assert result["value"] == 42, f"Expected 42, got {result}"
        print(f"  7 * 6 = {result['value']}  OK")

        result = con.execute(
            definitions='variable sval("hello");',
            hsl_code='StrReplace(sval, "hello", "world"); AddJsonVariable(___JSON___, sval, "value");',
        )
        assert result["value"] == "world", f"Expected 'world', got {result}"
        print(f"  StrReplace -> '{result['value']}'  OK")

        try:
            con.execute(hsl_code="UndefinedFunction12345XYZ();")
            print("  ERROR: should have raised VenusError")
        except VenusError as e:
            print(f"  VenusError raised correctly: {e.description[:60]}")

        print("\nAll checks passed.")

except VenusTransportError as e:
    print(f"Transport error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"Unexpected error: {type(e).__name__}: {e}")
    sys.exit(1)
finally:
    trace = get_latest_trace()
    if trace:
        print(f"\n--- Latest trace file: {trace} ---")
        print(trace.read_text(encoding="utf-8", errors="replace"))
    else:
        print("\nNo trace file found.")
