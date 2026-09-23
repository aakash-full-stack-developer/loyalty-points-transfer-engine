from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUserId, SessionDep
from app.api.schemas import AccountList, AccountOut, ProblemDetails
from app.db.models import Account, Program
from app.domain.enums import OwnerType
from app.domain.masking import mask_member_id

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])


@router.get(
    "",
    summary="Current user's balances in every program",
    responses={401: {"model": ProblemDetails, "description": "Missing X-User-Id"}},
)
async def list_accounts(user_id: CurrentUserId, session: SessionDep) -> AccountList:
    rows = await session.execute(
        select(Account, Program)
        .join(Program, Program.id == Account.program_id)
        .where(Account.owner_type == OwnerType.USER, Account.user_id == user_id)
        .order_by(Program.type, Program.code)
    )
    return AccountList(
        user_id=user_id,
        data=[
            AccountOut(
                program=program.code,
                program_name=program.name,
                program_type=program.type,
                balance=account.balance,
                external_member_id=mask_member_id(account.external_member_id),
                updated_at=account.updated_at,
            )
            for account, program in rows.tuples()
        ],
    )
