from fastapi import Depends, HTTPException

from .middleware import get_current_user
from .models.User import User


def require_platform_admin(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_platform_admin:
        raise HTTPException(status_code=403, detail="Platform admins only")
    return current_user
