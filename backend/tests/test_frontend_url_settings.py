from backend.config import Settings


def test_frontend_urls_loads_unprefixed_value_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "FRONTEND_URLS=http://localhost:3000,http://127.0.0.1:3000\n",
        encoding="utf-8",
    )

    config = Settings(_env_file=env_file)

    assert config.frontend_urls == "http://localhost:3000,http://127.0.0.1:3000"
