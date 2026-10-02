from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
import html
import logging
import re
import time
from typing import Callable

from app.core.logging import log_event
from app.core.settings import Settings
from app.llm.client import PerplexityClient, PerplexityCompletion
from app.llm.parsing import (
    ArticleParseError,
    ParsedArticle,
    derive_tags,
    format_article_block,
    normalize_summary,
    normalize_tags,
    normalize_title,
    parse_article,
)
from app.llm.prompts import ArticleRequest, build_revision_prompt, build_system_prompt, build_user_prompt
from app.llm.sanitize import SanitizedHtml, sanitize_html, strip_presentation
from app.llm.sources import (
    MAX_FOOTER_SOURCES,
    LinkPolicy,
    Source,
    host_of,
    is_official_host,
    merge_sources,
    rank_sources,
    render_footer,
    usable_sources,
)
from app.llm.style import StyleProfile, load_style_profile
from app.llm.validation import ERROR, Issue, score, validate_article

_logger = logging.getLogger("tistory_automation")
KST = timezone(timedelta(hours=9))  # 한국은 서머타임이 없어 고정 오프셋으로 충분하다
MAX_SEED_SOURCES = 8


@dataclass(frozen=True)
class GeneratedArticle:
    title: str
    body_html: str
    tags: list[str]
    summary: str = ""
    sources: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mode: str = ""
    blueprint: str = ""
    revisions: int = 0


@dataclass(frozen=True)
class PreparedPrompts:
    request: ArticleRequest
    profile: StyleProfile
    system: str
    user: str
    today: date
    seed: list[Source]


@dataclass(frozen=True)
class _Draft:
    title: str
    summary: str
    tags: list[str]
    body: SanitizedHtml
    source_ids: tuple[int, ...] = ()
    cited: tuple[Source, ...] = ()  # source_ids를 검색 결과에서 찾은 출처


def _resolve_cited(source_ids: tuple[int, ...], results: list[dict]) -> tuple[Source, ...]:
    """<article_sources>의 번호를 검색 결과(results[].id)에서 찾아 출처로 바꾼다. 없는 번호는 무시한다."""
    by_id: dict[int, dict] = {}
    for result in results:
        number = result.get("id")
        if isinstance(number, int) and not isinstance(number, bool):
            by_id.setdefault(number, result)
    return tuple(merge_sources([by_id[number] for number in source_ids if number in by_id]))


_HREF = re.compile(r'<a href="([^"]+)"')


def _external_hosts(draft: _Draft) -> list[str]:
    """본문 링크 중 공식 기관도, 글의 근거로 밝힌 출처의 사이트도 아닌 호스트. 사람이 공개 전에 확인할 대상이다."""
    cited_hosts = {host_of(source.url) for source in draft.cited}
    hosts: list[str] = []
    for href in _HREF.findall(draft.body.html):
        host = host_of(html.unescape(href))
        if host and host not in cited_hosts and not is_official_host(host) and host not in hosts:
            hosts.append(host)
    return hosts


class ArticleGenerator:
    """Perplexity로 글을 쓰고, 정제·검증·(필요하면) 재작성까지 마친 글을 돌려준다.

    1) 스타일 규칙(YAML)과 사전 조사 자료를 담은 프롬프트로 웹 검색 기반 초안을 쓴다.
    2) 출력을 해석하고, 본문 HTML을 허용 목록으로 정제하며, 검증된 출처가 아닌 링크를 걷어낸다.
    3) 규칙 위반(error)이 있으면 그 항목만 고치는 재작성을 최대 article_max_revisions회 요청한다.
    4) 검증된 출처 목록과 확인일 안내를 글 끝에 붙인다.
    """

    def __init__(self, settings: Settings, client: PerplexityClient | None = None) -> None:
        self.settings = settings
        self.client = client or PerplexityClient(settings, timeout_seconds=settings.article_timeout_seconds)

    def prepare(
        self,
        topic: str,
        *,
        category: str | None = None,
        reason: str | None = None,
        sources: list[dict[str, str]] | None = None,
        today: date | None = None,
    ) -> PreparedPrompts:
        """실제로 전송할 프롬프트를 만든다. generate()와 show-prompt 명령이 같은 경로를 쓴다."""
        topic = topic.strip()
        if not topic:
            raise ValueError("포스팅 주제가 비어 있습니다.")
        today = today or datetime.now(KST).date()
        seed = merge_sources(sources or ())
        reason = (reason or "").strip() or None
        request = ArticleRequest(
            topic=topic,
            category=(category or "").strip() or None,
            reason=reason,
            sources=tuple(usable_sources(seed)[:MAX_SEED_SOURCES]),
            # 이유·출처가 함께 오면 주제 탐색이 만든 제목형 문장이다(직접 입력한 키워드형 주제와 구분).
            title_seed=bool(reason or seed),
        )
        profile = load_style_profile(
            self.settings.article_style_rules_path,
            topic,
            request.category,
            title_style=self.settings.article_title_style,
        )
        return PreparedPrompts(
            request=request,
            profile=profile,
            system=build_system_prompt(profile),
            user=build_user_prompt(request, profile, today),
            today=today,
            seed=seed,
        )

    def generate(
        self,
        topic: str,
        *,
        category: str | None = None,
        reason: str | None = None,
        sources: list[dict[str, str]] | None = None,
        on_progress: Callable[[str], None] | None = None,
        today: date | None = None,
    ) -> GeneratedArticle:
        """주제로 글을 생성한다. on_progress(stage)는 각 LLM 호출 직전에 호출된다(진행 신호용)."""
        prepared = self.prepare(topic, category=category, reason=reason, sources=sources, today=today)
        request, profile, system, today, seed = (
            prepared.request, prepared.profile, prepared.system, prepared.today, prepared.seed
        )

        self._notify(on_progress, "write")
        completion = self._complete(
            [{"role": "system", "content": system}, {"role": "user", "content": prepared.user}],
            stage="write",
        )
        write_results = merge_sources(completion.search_results)  # 이번 검색에서 실제로 확인된 출처
        known = merge_sources(seed, write_results)  # 본문 링크를 허용하는 범위(주제 탐색 때의 출처 포함)
        draft = self._finalize(completion, request, profile, known, completion.search_results)
        issues = self._validate(draft, profile)

        notes: list[str] = []
        revisions = 0
        for _ in range(self.settings.article_max_revisions):
            errors = [issue for issue in issues if issue.severity == ERROR]
            if not errors:
                break
            self._notify(on_progress, "revise")
            block = format_article_block(
                draft.title, draft.summary, draft.tags, strip_presentation(draft.body.html), draft.source_ids
            )
            try:
                revised_completion = self._complete(
                    [{"role": "system", "content": system}, {"role": "user", "content": build_revision_prompt(block, errors)}],
                    stage="revise",
                )
                known = merge_sources(known, revised_completion.search_results)
                # 수정 요청은 초안의 <article_sources> 번호를 그대로 돌려주므로, 번호는 초안을 쓸 때의 검색 결과로 해석한다.
                revised = self._finalize(revised_completion, request, profile, known, completion.search_results)
            except Exception as error:  # 수정은 선택 단계다. 어떤 이유로든 실패하면 이미 비용을 낸 초안을 버리지 않고 그대로 쓴다
                _logger.warning("article revision failed (%s): %s", type(error).__name__, error)
                notes.append(f"수정 요청에 실패해 초안을 그대로 사용했습니다: {error}")
                break
            revisions += 1
            if not revised.source_ids and draft.source_ids:
                # 수정본이 <article_sources>를 빠뜨리면 푸터가 검색 결과 전체로 넓어지므로, 초안의 근거 번호를 이어 쓴다.
                revised = replace(revised, source_ids=draft.source_ids, cited=draft.cited)
            revised_issues = self._validate(revised, profile)
            if score(revised_issues) >= score(issues):
                notes.append("수정본이 초안보다 나아지지 않아 초안을 유지했습니다.")
                break
            draft, issues = revised, revised_issues

        # 푸터는 모델이 근거로 밝힌 검색 결과를 우선하고, 밝히지 않았으면 이번 검색 결과 전체를 쓴다.
        # 주제 탐색 때의 출처(seed)는 번호 매핑이 어긋났을 수 있어 독자에게 보이는 목록에는 넣지 않는다.
        cited_sources = rank_sources(draft.cited)
        fallback_sources = rank_sources(write_results)
        if not cited_sources:
            notes.append(
                "글의 근거 출처 번호(<article_sources>)를 확인하지 못해 이번 검색 결과 전체를 참고 자료로 붙였습니다."
                if fallback_sources
                else "글의 근거 출처 번호(<article_sources>)를 확인하지 못했고 참고 자료로 쓸 수 있는 검색 결과도 없습니다."
            )
        footer_sources = (cited_sources or fallback_sources)[:MAX_FOOTER_SOURCES]
        footer = render_footer(footer_sources, today, profile.mode)
        warnings = self._warnings(issues, draft, profile, notes)
        log_event(
            _logger,
            "article_generated",
            topic=request.topic,
            mode=profile.mode,
            blueprint=profile.blueprint,
            title_style=profile.title_style,
            chars=len(draft.body.text),
            h2=draft.body.h2_count,
            revisions=revisions,
            issues=[issue.code for issue in issues],
        )
        return GeneratedArticle(
            title=draft.title,
            body_html=f"{draft.body.html}\n\n{footer}",
            tags=draft.tags,
            summary=draft.summary,
            sources=[{"title": source.title, "url": source.url} for source in footer_sources],
            warnings=warnings,
            mode=profile.mode,
            blueprint=profile.blueprint,
            revisions=revisions,
        )

    def _complete(self, messages: list[dict[str, str]], *, stage: str) -> PerplexityCompletion:
        started = time.monotonic()
        completion = self.client.completion_response(
            messages,
            temperature=self.settings.article_temperature,
            max_tokens=self.settings.article_max_tokens,
            model=self.settings.article_model.strip() or None,
        )
        usage = completion.usage
        cost = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        log_event(
            _logger,
            "article_llm_call",
            stage=stage,
            model=completion.model,
            seconds=round(time.monotonic() - started, 1),
            finish_reason=completion.finish_reason,
            input_tokens=usage.get("input_tokens", usage.get("prompt_tokens")),
            output_tokens=usage.get("output_tokens", usage.get("completion_tokens")),
            cost_usd=cost.get("total_cost"),
            search_results=len(completion.search_results),
        )
        return completion

    def _finalize(
        self,
        completion: PerplexityCompletion,
        request: ArticleRequest,
        profile: StyleProfile,
        known: list[Source],
        results: list[dict],
    ) -> _Draft:
        parsed: ParsedArticle = parse_article(completion.content, truncated=completion.finish_reason == "length")
        title = normalize_title(parsed.title)
        policy = LinkPolicy.from_sources(usable_sources(known))
        body = sanitize_html(parsed.body_html, title=title, policy=policy)
        if not body.text:
            raise ArticleParseError("응답 본문이 비어 있습니다.")
        return _Draft(
            title=title,
            summary=normalize_summary(parsed.summary),
            tags=normalize_tags(parsed.tags) or derive_tags(request.topic, request.category),
            body=body,
            source_ids=parsed.source_ids,
            cited=_resolve_cited(parsed.source_ids, results),
        )

    @staticmethod
    def _validate(draft: _Draft, profile: StyleProfile) -> list[Issue]:
        return validate_article(title=draft.title, summary=draft.summary, tags=draft.tags, body=draft.body, profile=profile)

    @staticmethod
    def _warnings(issues: list[Issue], draft: _Draft, profile: StyleProfile, notes: list[str]) -> list[str]:
        warnings = [issue.message for issue in issues]
        if draft.body.removed_links:
            warnings.append(f"검증되지 않은 링크 {len(draft.body.removed_links)}개를 제거했습니다(링크 텍스트는 남겼습니다).")
        hosts = _external_hosts(draft)
        if hosts:
            warnings.append(f"본문에 공식 기관이 아닌 외부 사이트 링크가 있습니다({', '.join(hosts[:5])}). 공개 전에 링크가 맞는지 확인하세요.")
        warnings.extend(draft.body.notes)
        warnings.extend(notes)
        if profile.fallback_reason:
            warnings.append(f"{profile.fallback_reason} 내장 기본 규칙으로 작성했습니다.")
        return warnings

    @staticmethod
    def _notify(callback: Callable[[str], None] | None, stage: str) -> None:
        if callback is None:
            return
        try:
            callback(stage)
        except Exception as error:  # 진행 신호 실패가 글 생성을 막으면 안 된다
            log_event(_logger, "article_progress_callback_failed", stage=stage, error=str(error))
