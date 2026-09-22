"""Daily traffic dataset source — STUB.

TODO(poc): wire to the real daily-dataset retrieval APIs once their shapes are
confirmed (open question 2 in poc-scope.md).
"""

from app.datasets.base import DatasetRef, DatasetSource


class DailyApiDatasetSource(DatasetSource):
    name = "daily_api"

    def list_available(self) -> list[DatasetRef]:
        raise NotImplementedError("daily-dataset API shapes not confirmed yet")

    def fetch(self, ref: DatasetRef, dest_dir: str) -> str:
        raise NotImplementedError("daily-dataset API shapes not confirmed yet")
