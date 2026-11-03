from typing import Optional

from clairvoyance.entities.context import config_ctx
from clairvoyance.entities.interfaces import IConfig


# pylint: disable=too-few-public-methods
class Config(IConfig):
    def __init__(self, bucket_size: Optional[int] = None) -> None:
        super().__init__()
        self._bucket_size: int = bucket_size or 64

        config_ctx.set(self)
