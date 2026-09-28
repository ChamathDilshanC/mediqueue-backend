"""Locally served documentation, generated from the live OpenAPI contract."""
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(include_in_schema=False)
STATIC = Path(__file__).parent / "static"


@router.get("/")
@router.get("/docs")
async def documentation():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
