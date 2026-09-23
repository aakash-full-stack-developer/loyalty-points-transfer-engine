from fastapi import APIRouter

from app.api.deps import QuoteServiceDep
from app.api.schemas import PROBLEM_RESPONSES, QuoteRequest, QuoteResponse, RateSnapshotOut

router = APIRouter(prefix="/v1/quotes", tags=["quotes"])


@router.post(
    "",
    summary="Price a transfer (no side effects)",
    responses=PROBLEM_RESPONSES,
)
async def create_quote(body: QuoteRequest, quotes: QuoteServiceDep) -> QuoteResponse:
    quote = await quotes.quote(body.source_program, body.destination_program, body.source_points)
    conversion = quote.conversion
    return QuoteResponse(
        source_program=quote.source_program,
        destination_program=quote.destination_program,
        source_points=conversion.source_points,
        base_points=conversion.base_points,
        bonus_points=conversion.bonus_points,
        destination_points=conversion.destination_points,
        rate=RateSnapshotOut(**conversion.snapshot.to_dict()),
        quoted_at=quote.quoted_at,
        expires_at=quote.expires_at,
    )
