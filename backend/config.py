import os

DATABASE_URL: str = os.getenv(
    "TEST_DATABASE_URL",
    os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5434/tochkamod",
    ),
)

B2B_TO_MOD_KEY: str = os.getenv("B2B_TO_MOD_KEY", "dev-service-key")
