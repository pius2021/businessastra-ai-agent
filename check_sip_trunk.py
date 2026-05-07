"""
Utility: List all SIP outbound trunks in your LiveKit project.
Run this to find the correct OUTBOUND_TRUNK_ID to configure.

Usage: python check_sip_trunk.py
"""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

LIVEKIT_URL    = os.getenv("LIVEKIT_URL", "")
LIVEKIT_KEY    = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

STORED_TRUNK   = "ST_7ce14f41-4700-4baf-b5fc-e29a7e556e42"  # current DB value


async def main():
    if not all([LIVEKIT_URL, LIVEKIT_KEY, LIVEKIT_SECRET]):
        print("ERROR: Missing LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET in .env")
        return

    from livekit import api as lk_api

    print(f"Connecting to: {LIVEKIT_URL}")
    print(f"Stored trunk:  {STORED_TRUNK}")
    print("-" * 60)

    try:
        lk = lk_api.LiveKitAPI(url=LIVEKIT_URL, api_key=LIVEKIT_KEY, api_secret=LIVEKIT_SECRET)
        trunks_resp = await lk.sip.list_sip_outbound_trunk(lk_api.ListSIPOutboundTrunkRequest())
        await lk.aclose()

        trunks = trunks_resp.items if hasattr(trunks_resp, "items") else []

        if not trunks:
            print("No SIP outbound trunks found in this LiveKit project.")
            print()
            print("ACTION REQUIRED:")
            print("  1. In the dashboard go to Settings > Vobiz SIP Telephony")
            print("  2. Fill in SIP Domain, Username, Password, Outbound Number")
            print("  3. Click [Save Vobiz Config] then [Create SIP Trunk]")
            print("  4. A new ST_... Trunk ID will be generated and saved automatically")
        else:
            print(f"Found {len(trunks)} SIP outbound trunk(s):\n")
            for t in trunks:
                match = " <-- USE THIS" if t.sip_trunk_id == STORED_TRUNK else ""
                print(f"  ID:      {t.sip_trunk_id}{match}")
                print(f"  Name:    {getattr(t, 'name', 'N/A')}")
                print(f"  Address: {getattr(t, 'address', 'N/A')}")
                print(f"  Numbers: {getattr(t, 'numbers', [])}")
                print()

            # Check if stored trunk exists
            found_ids = [t.sip_trunk_id for t in trunks]
            if STORED_TRUNK not in found_ids:
                print(f"WARNING: Stored trunk '{STORED_TRUNK}' NOT found in this LiveKit project!")
                print()
                if trunks:
                    correct = trunks[0].sip_trunk_id
                    print(f"RECOMMENDATION: Update OUTBOUND_TRUNK_ID to: {correct}")
                    print(f"  Either update it in the Settings UI or update the DB directly.")

    except Exception as exc:
        print(f"ERROR calling LiveKit SIP API: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
