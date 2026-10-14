from sqlalchemy.orm import sessionmaker

from wcm.models import RunStatus
from wcm.worker import claim_next, enqueue


def test_claim_next_takes_oldest_and_marks_running(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        first = enqueue(s, "A")
        enqueue(s, "B")
        s.commit()
    with factory() as s:
        run = claim_next(s)
        s.commit()
        assert run.id == first.id and run.status == RunStatus.running
    with factory() as s:
        assert claim_next(s).title == "B"
        s.commit()
        assert claim_next(s) is None
