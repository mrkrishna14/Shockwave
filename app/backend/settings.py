"""Validated local platform configuration; no credentials are loaded in fixture mode."""
import os
from pathlib import Path
from pydantic import BaseModel, Field, field_validator


class Settings(BaseModel):
    database: Path = Path('shockwave.db')
    origin: str = 'http://127.0.0.1:3000'
    demo: bool = False
    secure_cookie: bool = False
    inline_jobs: bool = True

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
                   inline_jobs=os.getenv('SHOCKWAVE_INLINE_JOBS','1')=='1')
