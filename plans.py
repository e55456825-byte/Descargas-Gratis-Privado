"""Planes de suscripción y límites del bot."""
from datetime import datetime, timedelta, timezone

PLANS = {
    "basico": {"name": "Básico", "hours": 8, "max_file_bytes": int(1.5 * 1024**3), "daily_uploads": 10, "days": 30},
    "pro": {"name": "Pro", "hours": 12, "max_file_bytes": int(6.5 * 1024**3), "daily_uploads": 25, "days": 30},
    "ultra": {"name": "Ultra", "hours": 24, "max_file_bytes": None, "daily_uploads": None, "days": 30},
}

def utcnow():
    return datetime.now(timezone.utc)

def expiry_from_now(plan: str) -> str:
    return (utcnow() + timedelta(days=PLANS[plan]["days"])).isoformat()

def get_active_plan(user: dict) -> str:
    plan = (user or {}).get("plan", "basico")
    expiry = (user or {}).get("plan_expires_at")
    if plan not in PLANS:
        return "basico"
    if expiry:
        try:
            if datetime.fromisoformat(expiry) <= utcnow():
                return "basico"
        except ValueError:
            return "basico"
    return plan
