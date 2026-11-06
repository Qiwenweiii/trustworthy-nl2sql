"""/api/ask 入参契约：只认 question，其余字段一律拒收（白名单纪律）。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AskRequest(BaseModel):
    # 白名单纪律：injected_sql/max_rows/today/timeout_s 等管线内部入参不许从 HTTP 进来
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=200)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question 去空后不能为空")
        return value
