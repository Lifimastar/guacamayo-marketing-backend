from fastapi import Request, HTTPException, Depends
from gotrue.types import User
from gotrue.errors import AuthApiError
from postgrest import APIError

from app.config import supabase, logger

async def get_current_user(request: Request) -> User:
    auth_header = request.headers.get('Authorization')
    if not auth_header:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    token = auth_header.split(' ')[1] if ' ' in auth_header else auth_header

    try:
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        if not user:
            raise HTTPException(status_code=401, detail="Invalid or expired token")
        return user
    except AuthApiError as e:
        logger.error(f"Supabase Auth Error verifying user token: {e}", exc_info=True)
        raise HTTPException(status_code=401, detail=f"Authentication error: {e.message}")

async def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    try:
        profile_response = supabase.from_('profiles').select('role').eq('id', current_user.id).single().execute()

        if not profile_response.data or profile_response.data.get('role') != 'admin':
            raise HTTPException(status_code=403, detail="User is not an administrator")

        logger.info(f"Admin endpoint accessed by admin user {current_user.id}")
        return current_user
    except APIError as e:
        logger.error(f"APIError en dependencia de admin: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Database query error: {e.message}")
    except Exception as e:
        logger.error(f"Error inesperado en dependencia de admin: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error verifying admin credentials")