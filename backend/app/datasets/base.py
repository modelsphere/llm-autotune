"""Dataset subsystem — SKELETON (poc-scope.md decision 5).

Daily-dataset retrieval APIs exist but their shapes are not confirmed yet.
This interface is the placeholder seam: once shapes are known, implement
DatasetSource for real and register dataset versions in the datasets table.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DatasetRef:
    name: str
    version: str
    meta: dict[str, Any] = field(default_factory=dict)


class DatasetSource(ABC):
    name: str = "base"

    @abstractmethod
    def list_available(self) -> list[DatasetRef]:
        """Enumerate datasets available from this source."""

    @abstractmethod
    def fetch(self, ref: DatasetRef, dest_dir: str) -> str:
        """Materialize a dataset locally; returns the local path."""
