"""Shared synthetic property inputs; never derived from a private corpus."""
from datetime import datetime,timezone
from hypothesis import strategies as st

@st.composite
def result_dicts(draw):
    """Generate a list of result dicts suitable for rerank()."""
    n = draw(st.integers(min_value=1, max_value=10))
    results = []
    for _ in range(n):
        created = draw(st.one_of(
            st.none(),
            st.datetimes(
                min_value=datetime(2020, 1, 1),
                max_value=datetime(2026, 3, 17),
                timezones=st.just(timezone.utc),
            ),
        ))
        results.append({
            "rrf_score": draw(st.floats(min_value=0.0, max_value=1.0)),
            "content": draw(st.text(min_size=0, max_size=200)),
            "title": draw(st.text(min_size=0, max_size=50)),
            "created_at": created,
            "type": draw(st.sampled_from([
                "idea", "synthesis", "insight", "decision",
                "source", "research", "question",
            ])),
            "access_count": draw(st.one_of(
                st.none(),
                st.integers(min_value=0, max_value=1000),
            )),
        })
    return results
