from __future__ import annotations

from datetime import date, datetime, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.content.internal_links import (
    attach_internal_links_to_body,
    get_internal_links_for_category,
    insert_before_footer,
    render_internal_links_block,
)
from app.llm.sources import render_footer, strip_footer
from app.db.models import (
    Article,
    ArticleStatus,
    ArticleVersion,
    Base,
    PublishJob,
    PublishStatus,
)


def test_render_internal_links_block_empty() -> None:
    # 빈 링크 목록 전달 시 빈 문자열 반환
    assert render_internal_links_block([]) == ""


def test_render_internal_links_block_with_links_and_escaping() -> None:
    links = [
        {"title": "교통사고 & 합의 가이드 <주의>", "url": "https://blog.tistory.com/1?a=1&b=2"},
        {"title": "실손보험 청구 요령", "url": "https://blog.tistory.com/2"},
    ]
    html = render_internal_links_block(links)

    # 1. 필수 HTML 요소 확인
    assert "<blockquote>" in html
    assert "📌 함께 읽으면 도움 되는 관련 추천 가이드" in html
    assert "<ul>" in html
    assert "</ul>" in html
    assert "</blockquote>" in html

    # 2. XSS 및 HTML 특수문자 이스케이프 검증
    assert "&amp; 합의 가이드 &lt;주의&gt;" in html
    assert 'href="https://blog.tistory.com/1?a=1&amp;b=2"' in html
    assert "실손보험 청구 요령" in html


@pytest.fixture
def memory_db_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _create_mock_post(
    session: Session,
    article_id: int,
    version_id: int,
    job_id: int,
    title: str,
    category: str,
    status: PublishStatus,
    url: str | None,
    created_at: datetime,
) -> None:
    article = Article(id=article_id, content_hash=f"hash_{article_id}", status=ArticleStatus.VERIFIED)
    session.add(article)

    version = ArticleVersion(
        id=version_id,
        article_id=article_id,
        version_number=1,
        title=title,
        body_html="<p>본문</p>",
        tags_json=["태그"],
        category=category,
        thumbnail_path="storage/test.png",
        content_hash=f"vhash_{version_id}",
        status=ArticleStatus.VERIFIED,
        created_at=created_at,
    )
    session.add(version)

    pjob = PublishJob(
        id=job_id,
        article_version_id=version_id,
        target_blog_name="test-blog",
        category=category,
        visibility="PRIVATE",
        status=status,
        result_url=url,
        created_at=created_at,
    )
    session.add(pjob)
    session.commit()


def test_get_internal_links_category_prioritization(memory_db_session: Session) -> None:
    # 1. 법률 글 2건 등록 (시간차 둠)
    _create_mock_post(
        memory_db_session,
        1, 1, 1,
        "교통사고 형사합의 절차 1단계",
        "법률·합의·분쟁",
        PublishStatus.VERIFIED,
        "https://blog.tistory.com/1",
        datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
    )
    _create_mock_post(
        memory_db_session,
        2, 2, 2,
        "음주운전 면허취소 구제 가이드",
        "법률·합의·분쟁",
        PublishStatus.VERIFIED,
        "https://blog.tistory.com/2",
        datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc),
    )

    # 2. 보험 글 1건 등록
    _create_mock_post(
        memory_db_session,
        3, 3, 3,
        "실손보험 도수치료 청구 팁",
        "보험·보상·청구",
        PublishStatus.VERIFIED,
        "https://blog.tistory.com/3",
        datetime(2026, 1, 3, 10, 0, tzinfo=timezone.utc),
    )

    # 3. 미발행/실패 글 등록 (제외 대상)
    _create_mock_post(
        memory_db_session,
        4, 4, 4,
        "미발행 초안",
        "법률·합의·분쟁",
        PublishStatus.PENDING,
        None,
        datetime(2026, 1, 4, 10, 0, tzinfo=timezone.utc),
    )

    # 검증 1: 동일 카테고리 최신순 조회 (limit=2)
    links = get_internal_links_for_category(
        memory_db_session, category="법률·합의·분쟁", limit=2
    )
    assert len(links) == 2
    assert links[0]["title"] == "음주운전 면허취소 구제 가이드"
    assert links[1]["title"] == "교통사고 형사합의 절차 1단계"

    # 검증 2: 동일 카테고리 글 부족 시, 주제가 겹치는(태그·제목 키워드) 다른 카테고리 글로만 보충 (limit=3)
    links_3 = get_internal_links_for_category(
        memory_db_session, category="법률·합의·분쟁", limit=3, tags=["보험금 청구"]
    )
    assert len(links_3) == 3
    assert links_3[0]["title"] == "음주운전 면허취소 구제 가이드"
    assert links_3[1]["title"] == "교통사고 형사합의 절차 1단계"
    assert links_3[2]["title"] == "실손보험 도수치료 청구 팁"

    # 검증 2-1: 겹치는 주제가 없으면 다른 카테고리 글을 섞지 않는다(토픽 클러스터 신호 보존)
    unrelated = get_internal_links_for_category(memory_db_session, category="법률·합의·분쟁", limit=3)
    assert [link["title"] for link in unrelated] == ["음주운전 면허취소 구제 가이드", "교통사고 형사합의 절차 1단계"]

    # 검증 3: exclude_title 지정 시 자기 자신 제외
    links_ex = get_internal_links_for_category(
        memory_db_session,
        category="법률·합의·분쟁",
        exclude_title="음주운전 면허취소 구제 가이드",
        limit=2,
        tags=["청구"],
    )
    assert len(links_ex) == 2
    assert links_ex[0]["title"] == "교통사고 형사합의 절차 1단계"
    assert links_ex[1]["title"] == "실손보험 도수치료 청구 팁"

    # 검증 4: limit 0 이하일 때 빈 목록
    assert get_internal_links_for_category(memory_db_session, category="법률·합의·분쟁", limit=0) == []


def test_attach_internal_links_to_body(memory_db_session: Session) -> None:
    body = "<p>원래 본문 내용입니다.</p>"

    # 1. DB에 기발행 글이 없을 때 본문 그대로 유지
    attached_empty = attach_internal_links_to_body(
        body, memory_db_session, category="법률·합의·분쟁"
    )
    assert attached_empty == body

    # 2. 기발행 글 추가 후 본문 결합 확인
    _create_mock_post(
        memory_db_session,
        1, 1, 1,
        "교통사고 합의 요령",
        "법률·합의·분쟁",
        PublishStatus.VERIFIED,
        "https://blog.tistory.com/100",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    attached = attach_internal_links_to_body(
        body, memory_db_session, category="법률·합의·분쟁", require_public=False
    )
    assert attached.startswith(body)
    assert "<blockquote>" in attached
    assert "교통사고 합의 요령" in attached
    assert "https://blog.tistory.com/100" in attached


def test_attach_links_only_public_posts_by_default(memory_db_session: Session) -> None:
    # 발행기는 비공개로만 올리므로, 기본값은 비로그인으로 열리는 글만 연결한다.
    body = "<p>본문</p>"
    for number, title in ((1, "공개로 바꾼 글"), (2, "아직 비공개인 글")):
        _create_mock_post(
            memory_db_session, number, number, number, title, "법률·합의·분쟁",
            PublishStatus.VERIFIED, f"https://blog.tistory.com/{number}",
            datetime(2026, 1, number, tzinfo=timezone.utc),
        )
    checked: list[str] = []

    def is_public(url: str) -> bool:
        checked.append(url)
        return url.endswith("/1")

    attached = attach_internal_links_to_body(body, memory_db_session, category="법률·합의·분쟁", is_public=is_public)

    assert "공개로 바꾼 글" in attached and "아직 비공개인 글" not in attached
    assert set(checked) == {"https://blog.tistory.com/1", "https://blog.tistory.com/2"}
    # 공개 여부를 증명하지 못하면(테스트 환경은 외부 접속이 막혀 있다) 링크를 넣지 않고 본문을 그대로 둔다.
    assert attach_internal_links_to_body(body, memory_db_session, category="법률·합의·분쟁") == body


def test_same_category_links_are_ranked_by_topic_overlap(memory_db_session: Session) -> None:
    _create_mock_post(
        memory_db_session, 1, 1, 1, "개인회생 신청 자격과 필요 서류", "대출·부채·금융",
        PublishStatus.VERIFIED, "https://blog.tistory.com/1", datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _create_mock_post(
        memory_db_session, 2, 2, 2, "전세대출 금리 비교", "대출·부채·금융",
        PublishStatus.VERIFIED, "https://blog.tistory.com/2", datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    links = get_internal_links_for_category(
        memory_db_session, category="대출·부채·금융", exclude_title="개인회생 기각 사유", tags=["개인회생"], limit=2
    )

    # 최신 글보다 주제가 겹치는 글이 먼저 온다
    assert [link["title"] for link in links] == ["개인회생 신청 자격과 필요 서류", "전세대출 금리 비교"]


def test_links_block_goes_before_the_generated_footer(memory_db_session: Session) -> None:
    footer = render_footer([], date(2026, 10, 2), "lifestyle")
    body = f"<h2>본문 소제목</h2>\n<p>내용</p>\n\n{footer}"
    _create_mock_post(
        memory_db_session, 1, 1, 1, "관련 글", "법률·합의·분쟁",
        PublishStatus.VERIFIED, "https://blog.tistory.com/1", datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    attached = attach_internal_links_to_body(body, memory_db_session, category="법률·합의·분쟁", require_public=False)

    assert attached.index("<blockquote>") < attached.index("<p>※")
    assert attached.rstrip().endswith(footer.rstrip())
    # 글 점검(audit)이 쓰는 strip_footer가 여전히 꼬리를 찾는다
    assert "<p>※" not in strip_footer(attached)


def test_insert_before_footer_without_footer_appends() -> None:
    assert insert_before_footer("<p>a</p>\n", "<blockquote>b</blockquote>") == "<p>a</p>\n\n<blockquote>b</blockquote>"
