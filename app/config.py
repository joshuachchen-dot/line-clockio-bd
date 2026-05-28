from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    debug: bool = False

    line_channel_access_token: str
    line_channel_secret: str
    liff_id: str = ""
    liff_channel_id: str = ""
    liff_channel_secret: str = ""

    @property
    def liff_enabled(self) -> bool:
        return bool(self.liff_id and self.liff_channel_id and self.liff_channel_secret)

    database_url: str
    mailgun_api_key: str = ""
    mailgun_from_email: str = ""

    @property
    def mailgun_enabled(self) -> bool:
        return bool(self.mailgun_api_key and self.mailgun_from_email and "@" in self.mailgun_from_email)
    session_secret_key: str
    app_base_url: str
    timezone: str = "Asia/Taipei"
    factory_machine_id: str = "0000000002"

    # Factory FTP export
    ftp_host: str = "61.219.81.20"
    ftp_user: str = ""
    ftp_password: str = ""
    ftp_remote_dir: str = "/"

    # Internal job endpoint — Cloud Scheduler must send this as Bearer token
    internal_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
