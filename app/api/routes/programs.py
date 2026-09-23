from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import SessionDep
from app.api.schemas import ProgramList, ProgramOut
from app.db.models import Program

router = APIRouter(prefix="/v1/programs", tags=["programs"])


@router.get("", summary="List loyalty and card programs")
async def list_programs(session: SessionDep) -> ProgramList:
    rows = await session.scalars(select(Program).order_by(Program.type, Program.code))
    return ProgramList(
        data=[
            ProgramOut(code=row.code, name=row.name, type=row.type, active=row.active)
            for row in rows
        ]
    )
