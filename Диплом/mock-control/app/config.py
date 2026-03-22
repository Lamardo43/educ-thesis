from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_url: str = ""
    redis_data_dir: str = "./data/redis"

    fernet_key_path: str = "./data/fernet.key"
    upload_temp_dir: str = "./data/uploads"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    def get_redis_url(self) -> str:
        if self.redis_url:
            return self.redis_url
        return f"redis://{self.redis_host}:{self.redis_port}/0"


settings = Settings()
