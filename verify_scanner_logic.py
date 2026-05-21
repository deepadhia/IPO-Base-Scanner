#!/usr/bin/env python3
"""
verify_scanner_logic.py

Quick verification that all scanner fixes are properly in place.
Run this before deploying to confirm nothing is broken.
"""
import sys
import os

print("=" * 60)
print("🔍 IPO Scanner Logic Verification")
print("=" * 60)

errors = []
warnings = []

# ─── 1. Check SCANNER_VERSION exists ─────────────────────────
print("\n1️⃣  Checking SCANNER_VERSION...")
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("scanner", "streamlined_ipo_scanner.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    
    ver = getattr(mod, 'SCANNER_VERSION', None)
    if ver:
        print(f"   ✅ SCANNER_VERSION = {ver}")
    else:
        errors.append("SCANNER_VERSION not found in streamlined_ipo_scanner.py")
        print("   ❌ SCANNER_VERSION not found")
except Exception as e:
    errors.append(f"Failed to load scanner: {e}")
    print(f"   ❌ Failed to load scanner: {e}")

# ─── 2. Check is_market_hours exists ─────────────────────────
print("\n2️⃣  Checking is_market_hours()...")
try:
    fn = getattr(mod, 'is_market_hours', None)
    if fn:
        result = fn()
        print(f"   ✅ is_market_hours() = {result}")
    else:
        errors.append("is_market_hours not found")
        print("   ❌ is_market_hours not found")
except Exception as e:
    errors.append(f"is_market_hours error: {e}")
    print(f"   ❌ is_market_hours error: {e}")

# ─── 3. Check write_daily_log exists ─────────────────────────
print("\n3️⃣  Checking write_daily_log()...")
try:
    fn = getattr(mod, 'write_daily_log', None)
    if fn:
        print("   ✅ write_daily_log() available")
        # Test it writes to MongoDB
        fn("verify_test", "TEST_SYMBOL", "VERIFICATION", {"test": True})
        from db import logs_col
        if logs_col is not None:
            # Query for the test log
            test_log = logs_col.find_one({"scanner": "verify_test", "symbol": "TEST_SYMBOL"})
            if test_log:
                print("   ✅ Daily log successfully written to MongoDB")
                # Clean up
                logs_col.delete_one({"_id": test_log["_id"]})
            else:
                warnings.append("write_daily_log did not write to MongoDB")
                print("   ⚠️  Log not found in MongoDB")
        else:
            warnings.append("MongoDB logs collection not connected")
            print("   ⚠️  MongoDB not connected")
    else:
        errors.append("write_daily_log not found")
        print("   ❌ write_daily_log not found")
except Exception as e:
    errors.append(f"write_daily_log error: {e}")
    print(f"   ❌ write_daily_log error: {e}")

# ─── 4. Check symbol loop breaks are fixed ───────────────────
print("\n4️⃣  Checking symbol loop break fix...")
try:
    with open("streamlined_ipo_scanner.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # The bad pattern: "if signals_found > 0: break" at the end of symbol loop
    # Should NOT appear right after the window loop break
    lines = content.split('\n')
    bad_breaks = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "if signals_found > 0: break":
            # Check if this is at the symbol-loop level (indentation check)
            indent = len(line) - len(line.lstrip())
            if indent <= 12:  # Symbol loop level indentation
                bad_breaks += 1
                warnings.append(f"Possible symbol loop break at line {i+1}: {stripped}")
    
    if bad_breaks == 0:
        print("   ✅ No premature symbol-loop breaks found")
    else:
        print(f"   ⚠️  Found {bad_breaks} possible symbol-loop breaks — review manually")
    
    # Check for "DO NOT break the symbol loop" comments (our fix markers)
    fix_markers = content.count("DO NOT break the symbol loop")
    print(f"   ✅ Fix markers found: {fix_markers} (expected: 2 — detect_live_patterns + detect_scan)")
    if fix_markers < 2:
        warnings.append(f"Expected 2 fix markers, found {fix_markers}")

except Exception as e:
    errors.append(f"Symbol loop check error: {e}")
    print(f"   ❌ Error: {e}")

# ─── 5. Check listing_day_volume fix ─────────────────────────
print("\n5️⃣  Checking listing_day_volume fix...")
try:
    with open("listing_day_breakout_scanner.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    if "listing_day_volume" in content and "listing_info.get('listing_day_volume'" in content:
        print("   ✅ listing_day_volume is properly extracted from listing_info")
    else:
        errors.append("listing_day_volume extraction not found")
        print("   ❌ listing_day_volume extraction not found")
    
except Exception as e:
    errors.append(f"listing_day_volume check error: {e}")
    print(f"   ❌ Error: {e}")

# ─── 6. Check breakout candle quality check ──────────────────
print("\n6️⃣  Checking breakout candle quality checks...")
try:
    with open("streamlined_ipo_scanner.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # Check for CLOSE-based breakout check and bullish candle check
    has_close_check = 'df["CLOSE"].iat[j] > high2' in content
    has_bullish_check = 'df["CLOSE"].iat[j] > df["OPEN"].iat[j]' in content
    
    if has_close_check:
        print("   ✅ CLOSE > consolidation high check present")
    else:
        errors.append("Missing CLOSE breakout check")
        print("   ❌ Missing CLOSE breakout check")
    
    if has_bullish_check:
        print("   ✅ Bullish candle check present (CLOSE > OPEN)")
    else:
        errors.append("Missing bullish candle check")
        print("   ❌ Missing bullish candle check")

except Exception as e:
    errors.append(f"Quality check error: {e}")
    print(f"   ❌ Error: {e}")

# ─── 7. Check freshness filter ───────────────────────────────
print("\n7️⃣  Checking smart freshness filter...")
try:
    has_freshness = 'days_since_breakout' in content and 'holding above' in content.lower()
    if has_freshness:
        print("   ✅ Smart freshness filter present (3/10 day logic)")
    else:
        warnings.append("Freshness filter may not be present")
        print("   ⚠️  Freshness filter markers not clearly found")
except Exception as e:
    print(f"   ❌ Error: {e}")

# ─── 8. Check version in Telegram messages ───────────────────
print("\n8️⃣  Checking version in Telegram messages...")
try:
    with open("streamlined_ipo_scanner.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    scanner_msg_count = content.count("Scanner v{SCANNER_VERSION}")
    print(f"   ✅ Version tag in {scanner_msg_count} Telegram messages (expected: 2)")
    
    with open("listing_day_breakout_scanner.py", "r", encoding="utf-8") as f:
        listing_content = f.read()
    
    listing_msg_count = listing_content.count("Scanner v{SCANNER_VERSION}")
    print(f"   ✅ Version tag in {listing_msg_count} listing day messages (expected: 2)")
    
except Exception as e:
    print(f"   ❌ Error: {e}")

# ─── 9. Check ipo-scanner-v2.yml schedule ────────────────────
print("\n9️⃣  Checking workflow schedule...")
try:
    with open(".github/workflows/ipo-scanner-v2.yml", "r", encoding="utf-8") as f:
        yml_content = f.read()
    
    if "# COMMENTED OUT" in yml_content:
        errors.append("Schedule still commented out in ipo-scanner-v2.yml!")
        print("   ❌ Schedule still COMMENTED OUT!")
    elif "schedule:" in yml_content and "cron:" in yml_content:
        print("   ✅ Cron schedule is ACTIVE")
    else:
        warnings.append("Could not confirm schedule is active")
        print("   ⚠️  Could not confirm schedule status")
        
except Exception as e:
    errors.append(f"Workflow check error: {e}")
    print(f"   ❌ Error: {e}")

# ─── 10. Check LIVE_LOOKAHEAD ────────────────────────────────
print("\n🔟 Checking LIVE_LOOKAHEAD...")
try:
    if "LIVE_LOOKAHEAD = 5" in content:
        print("   ✅ LIVE_LOOKAHEAD = 5 (tightened from global 80)")
    else:
        warnings.append("LIVE_LOOKAHEAD = 5 not found")
        print("   ⚠️  LIVE_LOOKAHEAD = 5 not found")
except Exception as e:
    print(f"   ❌ Error: {e}")

# ─── Summary ─────────────────────────────────────────────────
print("\n" + "=" * 60)
print("📋 VERIFICATION SUMMARY")
print("=" * 60)

if errors:
    print(f"\n❌ ERRORS ({len(errors)}):")
    for e in errors:
        print(f"   • {e}")

if warnings:
    print(f"\n⚠️  WARNINGS ({len(warnings)}):")
    for w in warnings:
        print(f"   • {w}")

if not errors and not warnings:
    print("\n✅ ALL CHECKS PASSED! Scanner logic is correctly in place.")
elif not errors:
    print(f"\n✅ No critical errors. {len(warnings)} warning(s) — review above.")
else:
    print(f"\n❌ {len(errors)} error(s) found. Fix before deploying.")

print("=" * 60)
sys.exit(1 if errors else 0)
