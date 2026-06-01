from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

class LifecycleTracker:
    def __init__(self, repo):
        self.repo = repo

    def record_daily_update(self, signal_id: str, entry_price: float, stop_price: float, current_price: float, date: datetime):
        """
        Records a daily snapshot of the trade's progress.
        """
        try:
            # Normalize date to start of day for the unique index (UTC midnight representing IST date)
            from datetime import timezone, timedelta
            ist = timezone(timedelta(hours=5, minutes=30))
            if date.tzinfo is None:
                date = date.replace(tzinfo=ist)
            ist_date = date.astimezone(ist).date()
            update_date = datetime.combine(ist_date, datetime.min.time(), tzinfo=timezone.utc)
            
            runup_pct = (current_price - entry_price) / entry_price
            # Drawdown is only captured if it's negative relative to entry
            drawdown_pct = min(runup_pct, 0.0)

            update_doc = {
                "signal_id": signal_id,
                "date": update_date,
                "close": round(float(current_price), 2),
                "runup_pct": round(float(runup_pct), 4),
                "drawdown_pct": round(float(drawdown_pct), 4),
                "hit_stop": current_price <= stop_price,
                "created_at": datetime.now(timezone.utc)
            }

            saved = self.repo.save_update(update_doc)
            if saved:
                logger.debug(f"📈 [Lifecycle] Recorded update for {signal_id} on {update_date.date()}")
            return saved
        except Exception as e:
            logger.error(f"❌ [Lifecycle] Failed to record update for {signal_id}: {e}")
            return False
