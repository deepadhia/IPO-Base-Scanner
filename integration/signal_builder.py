from core.models import Signal
from enrichment.engine import EnrichmentEngine
from datetime import datetime, timezone
import pandas as pd

class SignalBuilder:
    """
    The Bridge: Converts raw scanner outputs into enriched institutional Signals.
    """
    def __init__(self):
        self.enricher = EnrichmentEngine()

    def build_signal(self, 
                     raw_payload: dict, 
                     candle: pd.Series, 
                     history: pd.DataFrame, 
                     base_candles: pd.DataFrame,
                      scanner_version: str,
                      is_complete_snapshot: bool = True,
                      incomplete_reasons: list[str] = None,
                      reference_date: datetime = None) -> Signal:
        """
        Takes raw data and builds the full Signal snapshot.
        """
        ref_dt = reference_date
        if not ref_dt:
            dt_val = candle['DATE']
            if isinstance(dt_val, str):
                ref_dt = pd.to_datetime(dt_val).to_pydatetime()
            elif hasattr(dt_val, 'to_pydatetime'):
                ref_dt = dt_val.to_pydatetime()
            else:
                ref_dt = dt_val
        enriched = self.enricher.enrich_signal(candle, history, base_candles, reference_date=ref_dt)
        
        # 2. Determine deterministic ID (symbol_date_setup_hash)
        breakout_date = candle['DATE']
        if isinstance(breakout_date, str):
            breakout_date = pd.to_datetime(breakout_date)
        
        # Ensure we have a datetime object
        if hasattr(breakout_date, 'to_pydatetime'):
            breakout_date = breakout_date.to_pydatetime()
            
        ds = breakout_date.strftime("%Y%m%d")
        symbol = raw_payload.get("symbol")
        
        # Robust field extraction
        entry = float(raw_payload.get("entry") or raw_payload.get("entry_price") or 0)
        stop = float(raw_payload.get("stop") or raw_payload.get("stop_loss") or 0)
        target = float(raw_payload.get("target") or raw_payload.get("target_price") or 0)
        
        prng = raw_payload.get("features", {}).get("prng") or raw_payload.get("consolidation_range_pct") or 0
        
        # Unique hash per setup
        import hashlib
        setup_raw = f"{symbol}_{ds}_{entry}_{prng}"
        setup_hash = hashlib.md5(setup_raw.encode()).hexdigest()[:8]
        signal_id = f"{symbol}_{ds}_{setup_hash}"
        
        # Reconciliation
        v1_entry = float(raw_payload.get("entry", 0))
        
        # 3. Construct the Signal object
        signal = Signal(
            signal_id=signal_id,
            symbol=symbol,
            signal_date=datetime.now(timezone.utc), # Entry time
            breakout_date=breakout_date,
            candle_timestamp=pd.to_datetime(candle['DATE']).to_pydatetime() if hasattr(candle['DATE'], 'to_pydatetime') else candle['DATE'],
            
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            
            features=raw_payload.get("metrics", {}),
            
            breakout_fingerprint=enriched.get("breakout", {}),
            base_quality=enriched.get("base", {}),
            market_context=enriched.get("market", {}),
            
            # Reconciliation
            source_log_id=raw_payload.get("log_id", "MISSING"),
            v1_entry_price=v1_entry,
            entry_price_delta_pct=round((entry - v1_entry) / v1_entry, 4) if v1_entry > 0 else 0.0,
            is_complete_snapshot=is_complete_snapshot,
            incomplete_reasons=incomplete_reasons or [],
            
            score_components={
                "tier_weight": raw_payload.get("tier_weight"),
                "volume_score": raw_payload.get("volume_score"),
                "base_score": raw_payload.get("base_score"),
                "momentum_score": raw_payload.get("momentum_score"),
                "total_score": raw_payload.get("signal_strength_score")
            },
            
            scanner=raw_payload.get("scanner", "ipo_base"),
            scanner_version=scanner_version,
            sector=raw_payload.get("sector", "Unknown"),
            industry=raw_payload.get("industry", "Unknown"),

            # Research Metadata (Phase 2)
            pattern_type=raw_payload.get("pattern_type", "UNKNOWN"),
            market_regime=raw_payload.get("market_regime", "UNKNOWN"),
            lifecycle_state="POSITION_ACTIVE",
            source_type=raw_payload.get("source_type", "UNKNOWN"),
            data_quality=raw_payload.get("data_quality", "UNKNOWN"),
            decision_snapshot=raw_payload.get("decision_snapshot", {})
        )
        
        return signal
