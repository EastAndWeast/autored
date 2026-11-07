"""v1 API 路由包"""
from fastapi import APIRouter
from .drafts import router as drafts_router
from .publish_jobs import router as jobs_router
from .media import router as media_router

router = APIRouter(prefix="/api/v1")
router.include_router(drafts_router)
router.include_router(jobs_router)
router.include_router(media_router)

