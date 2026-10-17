"""Validated local platform configuration. Only SMTP credentials are read, and only when set."""
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator


class Settings(BaseModel):
    database: Path = Path('shockwave.db')
    origin: str = 'http://127.0.0.1:3000'
    demo: bool = False
    secure_cookie: bool = False
    inline_jobs: bool = True
    # 'integrated' runs the owners' real modules (backend.integrated); 'fixture' is DemoAdapters.
    adapters: Literal['fixture', 'integrated'] = 'fixture'
    work_dir: Path = Path('.shockwave-integrated')
    # Invitation notifications. Without an SMTP host, demo mode uses the development inbox.
    smtp_host: str = ''
    smtp_port: int = 587
    smtp_username: str = ''
    smtp_password: str = ''
    smtp_from: str = ''
    smtp_starttls: bool = True
    app_url: str = ''

    @property
    def smtp_configured(self): return bool(self.smtp_host and self.smtp_from)

    @field_validator('origin')
    @classmethod
    def valid_origin(cls,value):
        if not value.startswith(('http://127.0.0.1:','http://localhost:','https://')) or '/' in value.split('://',1)[1]:
            raise ValueError('SHOCKWAVE_ORIGIN must be an origin without a path')
        return value

    @classmethod
    def from_env(cls):
        return cls(database=Path(os.getenv('SHOCKWAVE_DB','shockwave.db')),
                   origin=os.getenv('SHOCKWAVE_ORIGIN','http://127.0.0.1:3000'),
                   demo=os.getenv('SHOCKWAVE_DEMO')=='1',
                   secure_cookie=os.getenv('SHOCKWAVE_SECURE_COOKIE')=='1',
                   inline_jobs=os.getenv('SHOCKWAVE_INLINE_JOBS','1')=='1',
                   adapters=os.getenv('SHOCKWAVE_ADAPTERS','fixture'),
                   work_dir=Path(os.getenv('SHOCKWAVE_WORK_DIR','.shockwave-integrated')),
                   smtp_host=os.getenv('SHOCKWAVE_SMTP_HOST',''),
                   smtp_port=int(os.getenv('SHOCKWAVE_SMTP_PORT','587')),
                   smtp_username=os.getenv('SHOCKWAVE_SMTP_USERNAME',''),
                   smtp_password=os.getenv('SHOCKWAVE_SMTP_PASSWORD',''),
                   smtp_from=os.getenv('SHOCKWAVE_SMTP_FROM',''),
                   smtp_starttls=os.getenv('SHOCKWAVE_SMTP_STARTTLS','1')=='1',
                   app_url=os.getenv('SHOCKWAVE_APP_URL',''))
