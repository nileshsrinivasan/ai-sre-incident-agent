import os


class Settings:
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://sre:sre@localhost:5432/sre")
    PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
    DEMO_SERVICE_URL = os.getenv("DEMO_SERVICE_URL", "http://localhost:8001")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
    VERIFY_WAIT_SECONDS = int(os.getenv("VERIFY_WAIT_SECONDS", "45"))
    CHECKPOINTER = os.getenv("CHECKPOINTER", "postgres")   # postgres | memory


settings = Settings()

# SAFETY ALLOWLIST: the ONLY actions the agent may ever request, and the ONLY targets for each.
ALLOWED_ACTIONS = {
    "restart_service": ["demo-service"],
    "rollback_deployment": ["demo-service"],
    "terminate_blocking_queries": ["demo_report_job"],
}
