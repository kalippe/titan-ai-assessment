import logging

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import get_session
from app.models import Tenant

logger = logging.getLogger(__name__)


async def resolve_tenant(
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
) -> str:
    """Validate that the tenant_id path parameter maps to a registered library branch.

    Raises 404 before any business logic runs if the slug is unknown, preventing
    FK violations from surfacing as 500 errors further down the stack.
    """
    result = await session.execute(select(Tenant).where(Tenant.slug == tenant_id))
    if result.scalar_one_or_none() is None:
        logger.warning("Unknown tenant: %r", tenant_id)
        raise HTTPException(
            status_code=404,
            detail=f"Tenant '{tenant_id}' not found. "
                   f"Use GET /api/tenants to see registered branches.",
        )
    return tenant_id
