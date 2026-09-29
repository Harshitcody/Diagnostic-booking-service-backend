import math
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.deps import get_current_admin
from app.models import DiagnosticCentre, DiagnosticTest, User
from app.schemas import (
    DiagnosticCentreCreate,
    DiagnosticCentreOut,
    DiagnosticTestCreate,
    DiagnosticTestOut,
    Page,
)

router = APIRouter(tags=["Diagnostic Centres & Tests"])


@router.post(
    "/centres/",
    response_model=DiagnosticCentreOut,
    status_code=status.HTTP_201_CREATED,
)
def create_centre(
    payload: DiagnosticCentreCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    centre = DiagnosticCentre(name=payload.name.strip(), location=payload.location.strip())
    db.add(centre)
    db.flush()  # get centre.id before creating child tests

    for t in payload.tests or []:
        db.add(DiagnosticTest(centre_id=centre.id, name=t.name.strip(), price=t.price))

    db.commit()
    db.refresh(centre)
    return centre


@router.get("/centres/", response_model=Page[DiagnosticCentreOut])
def list_centres(
    location: Optional[str] = Query(None, description="Filter by (partial, case-insensitive) location"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(DiagnosticCentre).options(joinedload(DiagnosticCentre.tests))
    if location:
        query = query.filter(DiagnosticCentre.location.ilike(f"%{location}%"))

    total = query.distinct().count()
    items = (
        query.order_by(DiagnosticCentre.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    pages = math.ceil(total / page_size) if total else 0
    return Page(items=items, total=total, page=page, page_size=page_size, pages=pages)


@router.get("/centres/{centre_id}", response_model=DiagnosticCentreOut)
def get_centre(centre_id: str, db: Session = Depends(get_db)):
    centre = (
        db.query(DiagnosticCentre)
        .options(joinedload(DiagnosticCentre.tests))
        .filter(DiagnosticCentre.id == centre_id)
        .first()
    )
    if not centre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Centre not found.")
    return centre


@router.post(
    "/centres/{centre_id}/tests",
    response_model=DiagnosticTestOut,
    status_code=status.HTTP_201_CREATED,
)
def add_test_to_centre(
    centre_id: str,
    payload: DiagnosticTestCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    centre = db.query(DiagnosticCentre).filter(DiagnosticCentre.id == centre_id).first()
    if not centre:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Centre not found.")

    existing = (
        db.query(DiagnosticTest)
        .filter(DiagnosticTest.centre_id == centre_id, DiagnosticTest.name == payload.name.strip())
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A test with this name already exists at this centre.",
        )

    test = DiagnosticTest(centre_id=centre_id, name=payload.name.strip(), price=payload.price)
    db.add(test)
    db.commit()
    db.refresh(test)
    return test


@router.get("/tests/", response_model=Page[DiagnosticTestOut])
def list_tests(
    centre_id: Optional[str] = Query(None),
    name: Optional[str] = Query(None, description="Filter by (partial, case-insensitive) test name"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(DiagnosticTest)
    if centre_id:
        query = query.filter(DiagnosticTest.centre_id == centre_id)
    if name:
        query = query.filter(DiagnosticTest.name.ilike(f"%{name}%"))

    total = query.count()
    items = (
        query.order_by(DiagnosticTest.name)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    pages = math.ceil(total / page_size) if total else 0
    return Page(items=items, total=total, page=page, page_size=page_size, pages=pages)


@router.get("/tests/{test_id}", response_model=DiagnosticTestOut)
def get_test(test_id: str, db: Session = Depends(get_db)):
    test = db.query(DiagnosticTest).filter(DiagnosticTest.id == test_id).first()
    if not test:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Test not found.")
    return test
