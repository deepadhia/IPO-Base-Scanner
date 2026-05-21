import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from core.models import Signal, SignalUpdate
from core.repository import MongoRepository
from integration.signal_builder import SignalBuilder
from enrichment.engine import EnrichmentEngine

def run_test_suite():
    print("[START] Initializing Institutional Infrastructure Validation...")
    
    # 1. Component Initialization
    try:
        repo = MongoRepository()
        builder = SignalBuilder()
        enricher = EnrichmentEngine()
        print("[OK] Components initialized successfully.")
    except Exception as e:
        print(f"[FAIL] Initialization Failed: {e}")
        return

    # 2. Enrichment Stress Test (Dry Run)
    print("\n[TEST] Testing Enrichment Resilience (Empty/Flat Data)...")
    try:
        # Mocking an 'impossible' flat candle
        mock_candle = pd.Series({
            'DATE': datetime.now().strftime('%Y-%m-%d'),
            'OPEN': 100.0, 'HIGH': 100.0, 'LOW': 100.0, 'CLOSE': 100.0, 'VOLUME': 0
        })
        mock_history = pd.DataFrame([mock_candle] * 5)
        
        # Test if it crashes on division by zero or empty history
        res = enricher.enrich_signal(mock_candle, mock_history, mock_history)
        print("[OK] Enrichment handled flat data without crashing.")
    except Exception as e:
        print(f"[FAIL] Enrichment Stress Test Failed: {e}")

    # 3. Binary Integrity & Failure Attribution Test
    print("\n[TEST] Testing Binary Integrity & Failure Attribution...")
    try:
        raw_payload = {
            "symbol": "TEST_INFRA",
            "entry": 100,
            "metrics": {"prng": 10},
            "log_id": "test_audit_123"
        }
        
        # Force a failure by passing incomplete enrichment
        incomplete_signal = builder.build_signal(
            raw_payload=raw_payload,
            candle=mock_candle,
            history=mock_history,
            base_candles=mock_history,
            scanner_version="TEST_AUDIT",
            is_complete_snapshot=False,
            incomplete_reasons=["market_fetch_failed", "insufficient_history"]
        )
        
        if not incomplete_signal.is_complete_snapshot and len(incomplete_signal.incomplete_reasons) == 2:
            print(f"[OK] Failure Attribution working: {incomplete_signal.incomplete_reasons}")
        else:
            print("[FAIL] Failure Attribution Logic Mismatch.")
    except Exception as e:
        print(f"[FAIL] Binary Integrity Test Failed: {e}")

    # 4. DB Write & Idempotency Test (The \"Double Write\" Shield)
    print("\n[TEST] Testing DB Write & Idempotency (Unique Index Check)...")
    test_id = f"TEST_AUDIT_{datetime.now().strftime('%Y%m%d')}_ABC"
    try:
        test_signal = Signal(
            signal_id=test_id,
            symbol="TEST",
            signal_date=datetime.now(timezone.utc),
            breakout_date=datetime.now(),
            candle_timestamp=datetime.now(),
            entry_price=100,
            stop_price=90,
            target_price=120,
            features={},
            breakout_fingerprint={},
            base_quality={},
            market_context={},
            source_log_id="audit_v1",
            v1_entry_price=100,
            entry_price_delta_pct=0.0,
            is_complete_snapshot=True,
            incomplete_reasons=[],
            score_components={},
            scanner="TEST",
            scanner_version="TEST"
        )
        
        # First Write
        repo.save_signal(test_signal)
        print("[OK] First write successful.")
        
        # Second Write (Should be blocked by Unique Index)
        second_write = repo.save_signal(test_signal)
        if not second_write:
            print("[OK] Idempotency check PASSED: Duplicate signal blocked by unique index.")
        else:
            print("[FAIL] Idempotency check FAILED: Duplicate signal allowed! Check MongoDB indexes.")
            
        # Clean up
        repo.signals_v2.delete_one({"signal_id": test_id})
        print("[OK] Test data cleaned up.")
    except Exception as e:
        print(f"[FAIL] DB Integrity Test Failed: {e}")

    print("\n[DONE] Validation Suite Complete.")
    print("--------------------------------------------------")
    print("-> If all '[OK]' are present, your pipes are sealed.")
    print("-> Proceed to your first live scan with confidence.")
    print("--------------------------------------------------")

if __name__ == "__main__":
    run_test_suite()
