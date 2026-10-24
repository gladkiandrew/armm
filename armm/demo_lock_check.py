#!/usr/bin/env python3
"""Runtime half of the v0.4 demo lock (acceptance test 1).

The static half lives in the Makefile's `demo-lock` target and in
test_autopilot.TestDemoLock. This script proves the lock at RUNTIME: a client
that is live in any respect must be refused, not corrected.
"""
import sys

from . import autopilot
from . import weex_client


def _client(**over):
    attrs = {"demo": True, "mode": "DEMO", "order_path": weex_client.DEMO_ORDER_PATH}
    attrs.update(over)
    return type("C", (), attrs)()


# Clients that must be REFUSED outright.
REJECT = [
    ("demo=False", _client(demo=False)),
    ("live order path", _client(order_path=weex_client.LIVE_ORDER_PATH)),
    ("mode=LIVE", _client(mode="LIVE")),
    ("demo=None", _client(demo=None)),
    ("demo='true' (a string, not the boolean)", _client(demo="true")),
    ("demo=1 (truthy but not True)", _client(demo=1)),
]


def main():
    for name, client in REJECT:
        try:
            autopilot.demo_client(client)
        except autopilot.AutopilotSafetyError:
            continue
        print(f"FAIL: autopilot accepted a client with {name}")
        return 1

    # assert_demo must reject a missing client too.
    try:
        autopilot.assert_demo(None)
        print("FAIL: assert_demo accepted None")
        return 1
    except autopilot.AutopilotSafetyError:
        pass

    # A genuinely demo client must be ACCEPTED, or the lock proves nothing.
    try:
        autopilot.demo_client(_client())
    except autopilot.AutopilotSafetyError as e:
        print(f"FAIL: a valid demo client was refused: {e}")
        return 1

    # Passing nothing is the default path: autopilot builds its own demo client.
    built = autopilot.demo_client()
    if built.demo is not True or built.order_path != weex_client.DEMO_ORDER_PATH:
        print("FAIL: the self-built client is not demo")
        return 1

    print(f"demo-lock: {len(REJECT)} live-ish clients refused, None refused, "
          f"a demo client accepted, self-built client routes to "
          f"{built.order_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
