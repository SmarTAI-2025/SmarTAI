"""Include this router in private_main ONLY (integration remains explicit)."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError

from backend.analytics.admin_adoption import AnalyticsRangeTooLarge, query_adoption
from backend.auth import require_admin
from backend.models import User
from backend.services.admin_monitoring import collect_monitoring

router = APIRouter(prefix="/admin", tags=["admin-observations"])


class AdoptionQuery(BaseModel):
    start: float = Field(gt=0, allow_inf_nan=False)
    end: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    timezone: str = Field(default="Asia/Singapore", max_length=64)


@router.get("/monitoring")
def monitoring(response: Response, current: User = Depends(require_admin)):
    response.headers["Cache-Control"] = "no-store"
    return collect_monitoring()


@router.post("/analytics/query")
def adoption(req: AdoptionQuery, response: Response, current: User = Depends(require_admin)):
    response.headers["Cache-Control"] = "no-store"
    now = time.time()
    try:
        return query_adoption(start=req.start, end=req.end if req.end is not None else now, timezone=req.timezone, now=now)
    except AnalyticsRangeTooLarge:
        raise HTTPException(422, detail={"code": "analytics_range_too_large", "message": "事件过多，请缩短查询区间。"}) from None
    except ValueError:
        raise HTTPException(422, detail={"code": "invalid_analytics_query", "message": "请选择有效时区及过去 93 天以内的时间区间。"}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={"code": "analytics_unavailable", "message": "统计数据暂不可用，请检查数据库与用量事件表。"}) from None
