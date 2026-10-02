from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from ..middleware import get_current_user
from ..models.User import User
from ..rate_limit import limiter, user_key
from ..services.unfurl import BlockedURL, unfurl
from ..settings import unfurl_rate_limit
from ..ws_schemas import MAX_URL_LENGTH, EmbedIn

router = APIRouter(tags=["unfurl"])


@router.get(
    "/unfurl",
    response_model=EmbedIn,
    responses={status.HTTP_204_NO_CONTENT: {"description": "Nothing to preview"}},
)
@limiter.limit(unfurl_rate_limit, key_func=user_key)
async def get_unfurl(
    request: Request,
    response: Response,
    url: str = Query(..., max_length=MAX_URL_LENGTH),
    current_user: User = Depends(get_current_user),
):
    """Link-preview metadata for a URL. 204 when the page has none or can't be
    fetched; 400 when the URL points somewhere the server must not fetch."""
    try:
        embed = await unfurl(url)
    except BlockedURL:
        raise HTTPException(status_code=400, detail="URL not allowed")
    if embed is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return embed
