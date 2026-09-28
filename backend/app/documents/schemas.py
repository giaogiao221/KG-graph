from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DocumentVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    document_id: UUID
    project_id: UUID
    version_number: int
    original_filename: str
    sha256: str
    size_bytes: int
    mime_type: str
    is_extractable: bool
    uploader_id: UUID
    created_at: datetime
