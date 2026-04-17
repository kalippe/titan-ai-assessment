import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import Tenant

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Tenants"])


class TenantRead:
    pass


@router.get(
    "/api/tenants",
    summary="List registered library branches",
    response_model=list[dict],
)
async def list_tenants(session: AsyncSession = Depends(get_session)):
    """Return all registered library branches in the consortium.

    Use the `slug` value as the `{tenant_id}` path parameter in all other endpoints.
    The list is seeded at startup — pass an unknown slug to any tenant-scoped endpoint
    and you will receive a 404.
    """
    result = await session.execute(select(Tenant).order_by(Tenant.slug))
    tenants = result.scalars().all()
    return [{"slug": t.slug, "name": t.name} for t in tenants]
